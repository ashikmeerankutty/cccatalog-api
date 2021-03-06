import logging as log
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers
from cccatalog.api.controllers.search_controller import get_providers
from cccatalog.api.serializers.oauth2_serializers import\
    OAuth2RegistrationSerializer, OAuth2RegistrationSuccessful, OAuth2KeyInfo
from drf_yasg.utils import swagger_auto_schema
from cccatalog.api.models import ContentProvider
from cccatalog.api.models import ThrottledApplication
from cccatalog.api.utils.throttle import ThreePerDay, OnePerSecond
from cccatalog.api.utils.oauth2_helper import get_token_info
from django.core.cache import cache

IDENTIFIER = 'provider_identifier'
NAME = 'provider_name'
FILTER = 'filter_content'
URL = 'domain_name'


class HealthCheck(APIView):
    """
    Returns a `200 OK` response if the server is running.

    This endpoint is used in production to ensure that the server should receive
    traffic. If no response is provided, the server is deregistered from the
    load balancer and destroyed.
    """
    swagger_schema = None

    def get(self, request, format=None):
        return Response('', status=200)


class AboutImageResponse(serializers.Serializer):
    """ The full image search response. """
    provider_name = serializers.CharField()
    image_count = serializers.IntegerField()
    display_name = serializers.CharField()
    provider_url = serializers.CharField()


class ImageStats(APIView):
    """
    List all providers in the Creative Commons image catalog, in addition to the
    number of images from each data source.
    """
    @swagger_auto_schema(operation_id='image_stats',
                         responses={
                             200: AboutImageResponse(many=True)
                         })
    def get(self, request, format=None):
        provider_data = ContentProvider \
            .objects \
            .values(IDENTIFIER, NAME, FILTER, URL)
        provider_table = {
            rec[IDENTIFIER]:
                (rec[NAME], rec[FILTER], rec[URL]) for rec in provider_data
        }
        providers = get_providers('image')
        response = []
        for provider in providers:
            if provider in provider_table:
                display_name, _filter, provider_url = provider_table[provider]
                if not _filter:
                    response.append(
                        {
                            'provider_name': provider,
                            'image_count': providers[provider],
                            'display_name': display_name,
                            'provider_url': provider_url
                        }
                    )
            else:
                msg = 'provider_identifier missing from content_provider' \
                      ' table: {}. Check for typos/omissions.'.format(provider)
                log.error(msg)
        return Response(status=200, data=response)


class Register(APIView):
    """
    Register for access to the API via OAuth2. Authenticated users have higher
    rate limits than anonymous users. Additionally, by identifying yourself,
    you can request Creative Commons to adjust your personal rate limit
    depending on your organization's needs.

    Upon registering, you will receive a `client_id` and `client_secret`, which
    you can then use to authenticate using the standard OAuth2 Client
    Credentials flow. You must keep `client_secret` confidential; anybody with
    your `client_secret` can impersonate your application.
    
    Example registration and authentication flow:

    First, register for a key.
    ```
    $ curl -XPOST -H "Content-Type: application/json" -d '{"name": "My amazing project", "description": "A description", "email": "example@example.com"}' https://api.creativecommons.engineering/oauth2/register
    {
        "client_secret" : "YhVjvIBc7TuRJSvO2wIi344ez5SEreXLksV7GjalLiKDpxfbiM8qfUb5sNvcwFOhBUVzGNdzmmHvfyt6yU3aGrN6TAbMW8EOkRMOwhyXkN1iDetmzMMcxLVELf00BR2e",
        "client_id" : "pm8GMaIXIhkjQ4iDfXLOvVUUcIKGYRnMlZYApbda",
        "name" : "My amazing project"
    }

    ```

    Now, exchange your client credentials for a token.
    ```
    $ curl -X POST -d "client_id=pm8GMaIXIhkjQ4iDfXLOvVUUcIKGYRnMlZYApbda&client_secret=YhVjvIBc7TuRJSvO2wIi344ez5SEreXLksV7GjalLiKDpxfbiM8qfUb5sNvcwFOhBUVzGNdzmmHvfyt6yU3aGrN6TAbMW8EOkRMOwhyXkN1iDetmzMMcxLVELf00BR2e&grant_type=client_credentials" https://api.creativecommons.engineering/oauth2/token/
    {
       "access_token" : "DLBYIcfnKfolaXKcmMC8RIDCavc2hW",
       "scope" : "read write groups",
       "expires_in" : 36000,
       "token_type" : "Bearer"
    }
    ```

    Include the `access_token` in the authorization header to use your key in
    your future API requests.

    ```
    $ curl -H "Authorization: Bearer DLBYIcfnKfolaXKcmMC8RIDCavc2hW" https://api.creativecommons.engineering/image/search?q=test
    ```

    **Be advised** that you can only make up to 3 registration requests per day.
    We ask that you only use a single API key per application; abuse of the
    registration process is easily detectable.
    """  # noqa
    throttle_classes = (ThreePerDay,)

    @swagger_auto_schema(operation_id='register_api_oauth2',
                         request_body=OAuth2RegistrationSerializer,
                         responses={
                             201: OAuth2RegistrationSuccessful
                         })
    def post(self, request, format=None):
        # Store the registration information the developer gave us.
        serialized = OAuth2RegistrationSerializer(data=request.data)
        if not serialized.is_valid():
            return Response(
                status=400,
                data=serialized.errors
            )
        else:
            serialized.save()

        # Produce a client ID, client secret, and authorize the application in
        # the OAuth2 backend.
        new_application = ThrottledApplication(
            name=serialized.validated_data['name'],
            skip_authorization=False,
            client_type='Confidential',
            authorization_grant_type='client-credentials'
        )
        new_application.save()
        # Give the user their newly created credentials.
        return Response(
            status=201,
            data={
                'client_id': new_application.client_id,
                'client_secret': new_application.client_secret,
                'name': new_application.name
            }
        )


class CheckRates(APIView):
    """
    Return information about the rate limit status of your API key.
    """
    throttle_classes = (OnePerSecond,)

    @swagger_auto_schema(operation_id='key_info',
                         responses={
                             200: OAuth2KeyInfo,
                             403: 'Forbidden'
                         })
    def get(self, request, format=None):
        if not request.auth:
            return Response(status=403, data='Forbidden')

        access_token = str(request.auth)
        client_id, rate_limit_model = get_token_info(access_token)

        if not client_id:
            return Response(status=403, data='Forbidden')

        throttle_type = rate_limit_model
        throttle_key = 'throttle_{scope}_{client_id}'
        if throttle_type == 'standard':
            sustained_throttle_key = throttle_key.format(
                scope='oauth2_client_credentials_sustained',
                client_id=client_id
            )
            burst_throttle_key = throttle_key.format(
                scope='oauth2_client_credentials_burst',
                client_id=client_id
            )
        elif throttle_type == 'enhanced':
            sustained_throttle_key = throttle_key.format(
                scope='enhanced_oauth2_client_credentials_sustained',
                client_id=client_id
            )
            burst_throttle_key = throttle_key.format(
                scope='enhanced_oauth2_client_credentials_burst',
                client_id=client_id
            )
        else:
            return Response(status=500, data='Unknown API key rate limit type')

        sustained_requests_list = cache.get(sustained_throttle_key)
        sustained_requests = \
            len(sustained_requests_list) if sustained_requests_list else None
        burst_requests_list = cache.get(burst_throttle_key)
        burst_requests = \
            len(burst_requests_list) if burst_requests_list else None

        response_data = {
            'requests_this_minute': burst_requests,
            'requests_today': sustained_requests,
            'rate_limit_model': throttle_type
        }
        return Response(status=200, data=response_data)
