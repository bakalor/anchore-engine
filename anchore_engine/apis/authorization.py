"""
API Authorization handlers and functions for use in API processing

"""

from abc import abstractmethod, ABC
import anchore_engine
from collections import namedtuple
from anchore_engine.subsys import logger
from connexion import request as request_proxy
from anchore_engine.apis.context import ApiRequestContextProxy
from yosai.core import Yosai, exceptions as auth_exceptions, UsernamePasswordToken
from anchore_engine.db import session_scope
import pkg_resources
import functools
from anchore_engine.common.helpers import make_response_error
from anchore_engine.apis.authentication import idp_factory, IdentityContext
from threading import RLock

# Global authorizer configured
_global_authorizer = None


class UnauthorizedError(Exception):
    def __init__(self, required_permissions):
        if type(required_permissions) != list:
            required_permissions = [required_permissions]

        required_permissions = [ perm.split(':') for perm in required_permissions]
        perm_str = ','.join('domain={} action={} target={}'.format(perm[0], perm[1], '*' if len(perm) == 2 else perm[2]) for perm in required_permissions)
        super(UnauthorizedError, self).__init__('Not authorized. Requires permissions: {}'.format(perm_str))
        self.required_permissions = required_permissions


class UnauthenticatedError(Exception):
    pass


Permission = namedtuple('Permission', ['domain', 'action', 'target'])


class LazyBoundValue(ABC):
    """
    A generic domain handler that supports lazy binding and injection
    """
    def __init__(self, value=None):
        self._value = value

    def bind(self, kwargs=None):
        """
        Bind the actual value for the authz domain if applicable
        :return:
        """
        pass

    @property
    def value(self):
        """
        Retrieves the value of the domain
        :return:
        """
        return self._value


class FunctionInjectedValue(LazyBoundValue):
    def __init__(self, load_fn):
        super().__init__(None)
        self.loader = load_fn

    def bind(self, kwargs=None):
        self._value = self.loader()


class RequestingAccountValue(FunctionInjectedValue):
    def __init__(self):
        super().__init__(lambda: ApiRequestContextProxy.namespace())


class ParameterBoundValue(LazyBoundValue):
    def __init__(self, parameter_name, default_value=None):
        super().__init__(default_value)
        self.param_name = parameter_name

    def bind(self, kwargs=None):
        self._value = kwargs.get(self.param_name) if kwargs else self._value


class AuthorizationHandler(ABC):
    def __init__(self, identity_provider_factory):
        self._idp_factory = identity_provider_factory

    @abstractmethod
    def load(self, configuration):
        """
        Deferred loader for the handler to avoid constructor issues

        :param configuration:
        :return:
        """

        pass

    @abstractmethod
    def authorize(self, identity: IdentityContext, permission_list):
        """
        Authorize the described permissions (all must pass) for the identity
        Where domain = account | 'system'
        action_type in ActionTypes


        :param identity: IdentityContext object for the entity to authorize
        :param permission_list: list of Permission objects that must all be allowed
        :return:
        """
        pass

    @abstractmethod
    def authenticate(self, request):
        """
        Authenticate a request (wsgi/flask), perform any login/auth and return the authenticated account, username tuple
        :return: (account, username)
        """
        pass

    def check_permissions(self, permission_s):
        """

        :param permission_s: List of permission tuples (domain, action, target) that must all be verified
        :return:
        """
        pass

    @abstractmethod
    def requires(self, permission_s: list):
        pass


class DbAuthorizationHandler(AuthorizationHandler):
    """
    Default authorization handler for service apis.

    """
    _yosai = None
    _config_lock = RLock()

    def load(self, configuration):
        with DbAuthorizationHandler._config_lock:
            conf_path = pkg_resources.resource_filename(anchore_engine.__name__, 'conf/default_yosai_settings.yaml')
            DbAuthorizationHandler._yosai = Yosai(file_path=conf_path)
            # Disable sessions, since the APIs are not session-based
            DbAuthorizationHandler._yosai.security_manager.subject_store.session_storage_evaluator.session_storage_enabled = False

    def authenticate(self, request):
        logger.debug('Authenticating with native auth handler')
        subject = Yosai.get_current_subject()

        if request.authorization:
            authc_token = UsernamePasswordToken(username=request.authorization.username,
                                                password=request.authorization.password, remember_me=False)

            subject.login(authc_token)
            user = subject.primary_identifier

            # Simple account lookup to ensure the context identity is complete
            try:
                with session_scope() as db_session:
                    idp = self._idp_factory.for_session(db_session)
                    identity, _ = idp.lookup_user(user)

                    logger.debug('Authc complete')
                    return identity
            except:
                logger.exception('Error looking up account for authenticated user')
                return None
        else:
            logger.debug('Anon auth complete')
            return IdentityContext(username=None, user_account=None, user_account_type=None)

    def authorize(self, identity: IdentityContext, permission_list):
        logger.debug('Authorizing with native auth handler: {}'.format(permission_list))

        subject = Yosai.get_current_subject()
        if subject.primary_identifier != identity.username:
            raise UnauthorizedError(permission_list)

        logger.debug('Checking permission: {}'.format(permission_list))
        try:
            subject.check_permission(permission_list, logical_operator=all)
        except (ValueError, auth_exceptions.UnauthorizedException) as ex:
            raise UnauthorizedError(required_permissions=permission_list)

        logger.debug('Passed check permission: {}'.format(permission_list))

    def requires(self, permission_s: list):
        """
        Decorator for convenience on access control on API operations

        Empty list for authc only

        :param permission_s: list of Permission objects

        :return:
        """

        def outer_wrapper(f):
            @functools.wraps(f)
            def inner_wrapper(*args, **kwargs):
                try:
                    with Yosai.context(self._yosai):
                        # Context Manager functions
                        try:
                            try:
                                identity = self.authenticate(request_proxy)
                                if not identity.username:
                                    raise UnauthenticatedError('Authentication Required')
                            except:
                                raise UnauthenticatedError('Authentication Required')

                            ApiRequestContextProxy.set_identity(identity)
                            permissions_final = []

                            # Bind all the permissions as needed
                            for perm in permission_s:
                                domain = perm.domain if perm.domain else '*'
                                action = perm.action if perm.action else '*'
                                target = perm.target if perm.target else '*'

                                if hasattr(domain, 'bind'):
                                    domain.bind(kwargs=kwargs)
                                    domain = domain.value

                                if hasattr(action, 'bind'):
                                    action.bind(kwargs=kwargs)
                                    action = action.value

                                if hasattr(target, 'bind'):
                                    target.bind(kwargs=kwargs)
                                    target = target.value

                                permissions_final.append(':'.join([domain, action, target]))

                            # Do the authz on the bound permissions
                            try:
                                self.authorize(identity, permissions_final)
                            except UnauthorizedError as ex:
                                raise
                            except Exception as e:
                                logger.exception('Error doing authz: {}'.format(e))
                                raise UnauthorizedError(permissions_final)

                            return f(*args, **kwargs)
                        finally:
                            # Teardown the request context
                            ApiRequestContextProxy.set_identity(None)

                except UnauthorizedError as ex:
                    return make_response_error(str(ex), in_httpcode=403), 403
                except UnauthenticatedError as ex:
                    return make_response_error('Unauthorized', in_httpcode=401), 401
                except Exception as ex:
                    logger.exception('Unexpected exception: {}'.format(ex))
                    return make_response_error('Internal error', in_httpcode=500), 500

            return inner_wrapper

        return outer_wrapper


class ExternalAuthorizationHandler(DbAuthorizationHandler):

    def init_domain(self, domain_name):
        pass

    def init_account(self, account_name, requesting_username):
        pass

    def load(self, configuration):
        with ExternalAuthorizationHandler._config_lock:
            conf_path = pkg_resources.resource_filename(anchore_engine.__name__, 'conf/external_authz_yosai_settings.yaml')
            ExternalAuthorizationHandler._yosai = Yosai(file_path=conf_path)

            # Disable sessions, since the APIs are not session-based
            ExternalAuthorizationHandler._yosai.security_manager.subject_store.session_storage_evaluator.session_storage_enabled = False


class InternalServiceAuthorizer(DbAuthorizationHandler):
    """
    Authz Handler optimized for internal services

    """

    def load(self, configuration):
        conf_path = pkg_resources.resource_filename(anchore_engine.__name__, 'conf/internal_authz_yosai_settings.yaml')
        self.yosai = Yosai(file_path=conf_path)

        # Disable sessions, since the APIs are not session-based
        self.yosai.security_manager.subject_store.session_storage_evaluator.session_storage_enabled = False


def init_authz_handler(configuration=None):
    global _global_authorizer
    handler_config = configuration.get('authorization_handler')
    if handler_config == 'native' or handler_config is None:
        handler = DbAuthorizationHandler(identity_provider_factory=idp_factory)
    elif handler_config == 'external':
        handler = ExternalAuthorizationHandler(identity_provider_factory=idp_factory)
    else:
        raise Exception('Unknown authorization handler: {}'.format(handler_config))

    handler.load(configuration)
    _global_authorizer = handler


def get_authorizer():
    return _global_authorizer
