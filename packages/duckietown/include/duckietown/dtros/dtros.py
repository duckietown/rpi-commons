import rospy
from copy import copy

from std_srvs.srv import SetBool, SetBoolResponse
from duckietown_msgs.srv import \
    NodeGetParamsList, \
    NodeGetParamsListResponse, \
    NodeRequestParamsUpdate, \
    NodeRequestParamsUpdateResponse
from duckietown_msgs.msg import NodeParameter
from duckietown.dtros.constants import \
    NODE_GET_PARAM_SERVICE_NAME, \
    NODE_REQUEST_PARAM_UPDATE_SERVICE_NAME, \
    NODE_SWITCH_SERVICE_NAME
from .dtparam import DTParam
from .constants import NodeHealth, ModuleType
from .diagnostics import DTROSDiagnostics


class DTROS(object):
    """
    Parent class for all Duckietown ROS nodes

    All Duckietown ROS nodes should inherit this class. This class provides
    some basic common functionality that most of the ROS nodes need. By keeping
    these arguments and methods in a parent class, we can ensure consistent and
    reliable behaviour of all ROS nodes in the Duckietown universe.

    In particular, the DTROS class provides:

    - Logging: DTROS provides the `log` method as a wrapper around the ROS logging
      services. It will automatically append the ROS node name to the message.
    - Parameters handling:  DTROS provides the `parameters` and `parametersChanged` attributes
      and automatically updates them if it detects a change in the Parameter Server.
    - Shutdown procedure: a common shutdown procedure for ROS nodes. Should be attached
      via `rospy.on_shutdown(nodeobject.onShutdown)`.
    - Switchable Subscribers and Publishers: `DTROS.publisher()` and `DTROS.subscriber()` returns
      modified subscribers and publishers that can be dynamically deactivated and reactivated
      by requesting `False` or `True` to the `~switch` service respectively.
    - Node deactivation and reactivation: through requesting `Falce` to the `~switch`
      service all subscribers and publishers obtained through `DTROS.publisher()` and `DTROS.subscriber()`
      will be deactivated and the `switch` attribute will be set to `False`. This switch can be
      used by computationally expensive parts of the node code that are not in callbacks in ordert to
      to pause their execution.

    Every children node should call the initializer of `DTROS`. This should be done
    by having the following line at the top of the children node `__init__` method::

        super(ChildrenNode, self).__init__(node_name='children_node_name')

    The DTROS initializer will:

    - Initialize the ROS node with name `node_name`
    - Setup the `node_name` attribute to the node name passed by ROS (using `rospy.get_name()`)
    - Add a `rospy.on_shutdown` hook to the node's `onShutdown` method
    - Initialize an empty `parameters` dictionary where all configurable ROS parameters should
      be stored. A boolean attribute `parametersChanged` is also initialized. This will be set to
      `True` when the `updateParameters` callback detects a change in a parameter value in the
      `ROS Parameter Server <https://wiki.ros.org/Parameter%20Server>`_ and changes the value
      of at least one parameter.
    - Start a recurrent timer that calls `updateParameters` regularly to
      check if any parameter has been updated
    - Setup a `~switch` service that can be used to deactivate and reactivate the node

    Args:
       node_name (:obj:`str`): a unique, descriptive name for the node that ROS will use
       parameters_update_period (:obj:`float`): how often to check for new parameters (in seconds). If
          it is 0, it will not run checks at all

    Attributes:
       node_name (:obj:`str`): the name of the node
       parameters (:obj:`dict`): a dictionary that holds pairs `('~param_name`: param_value)`. Note that
          parameters should be given in private namespace (starting with `~`)
       parametersChanged (:obj:`bool`): a boolean indticator if the
       is_shutdown (:obj:`bool`): will be set to `True` when the `onShutdown` method is called
       switch (:obj:`bool`): flag determining whether the node is active or not. Read-only, controlled through
          the `~switch` service

    Service:
        ~switch:
            Switches the node between active state and inative state.

            input:
                data ('bool`): The desired state. `True` for active, `False` for inactive.

            outputs:
                success (`bool`): `True` if the call succeeded
                message (`str`): Used to give details about success

    """

    def __init__(self,
                 node_name,
                 # DT parameters from here
                 node_type=ModuleType.GENERIC):
        # configure singleton
        if rospy.__instance__ is not None:
            raise RuntimeError('You cannot instantiate two objects of type DTROS')
        rospy.__instance__ = self
        if not isinstance(node_type, ModuleType):
            raise ValueError(
                'DTROS \'node_type\' parameter must be of type \'duckietown.ModuleType\', '
                'got %s instead.' % str(type(node_type))
            )
        # Initialize the node
        rospy.init_node(node_name, __dtros__=True)
        self.node_name = rospy.get_name()
        self.node_type = node_type
        self.log('Initializing...')
        self.is_shutdown = False
        self._health = NodeHealth.UNKNOWN
        self._health_reason = None

        # Initialize parameters handling
        self._parameters = dict()

        # Handle publishers, subscribers, and the state switch
        self._switch = True
        self._subscribers = list()
        self._publishers = list()
        # create switch service for node
        self.srv_switch = rospy.Service(
            "~%s" % NODE_SWITCH_SERVICE_NAME,
            SetBool, self._srv_switch
        )
        # create services to manage parameters
        self._srv_get_params = rospy.Service(
            "~%s" % NODE_GET_PARAM_SERVICE_NAME,
            NodeGetParamsList, self._srv_get_params_list
        )
        self._srv_request_params_update = rospy.Service(
            "~%s" % NODE_REQUEST_PARAM_UPDATE_SERVICE_NAME,
            NodeRequestParamsUpdate, self._srv_request_param_update
        )
        # register node against the diagnostics manager
        if DTROSDiagnostics.enabled():
            DTROSDiagnostics.getInstance().register_node(
                self.node_name,
                health=self._health
            )
        # mark node as healthy and STARTING
        self.set_health(NodeHealth.STARTING)
        # register shutdown callback
        rospy.on_shutdown(self.onShutdown)

    # Read-only properties for the private attributes
    @property
    def switch(self):
        """Current state of the node on/off switch"""
        return self._switch

    @property
    def parameters(self):
        """List of parameters"""
        return copy(list(self._parameters.values()))

    @property
    def subscribers(self):
        """A list of all the subscribers of the node"""
        return self._subscribers

    @property
    def publishers(self):
        """A list of all the publishers of the node"""
        return self._publishers

    def set_health(self, health, reason=None):
        if not isinstance(health, NodeHealth):
            raise ValueError('Argument \'health\' must be of type duckietown.NodeHealth. '
                             'Got %s instead' % str(type(health)))
        self.log('Health status changed [%s] -> [%s]' % (self._health.name, health.name))
        self._health = health
        self._health_reason = None if reason is None else str(reason)
        # update node health in the diagnostics manager
        if DTROSDiagnostics.enabled():
            DTROSDiagnostics.getInstance().update_node(
                health=self._health,
                health_reason=self._health_reason
            )

    def log(self, msg, type='info'):
        """ Passes a logging message to the ROS logging methods.

        Attaches the ros name to the beginning of the message and passes it to
        a suitable ROS logging method. Use the `type` argument to select the method
        to be used (`debug` for `rospy.logdebug`,
        `info` for `rospy.loginfo`, `warn` for `rospy.logwarn`,
        `err` for `rospy.logerr`, `fatal` for `rospy.logfatal`).

        Args:
            msg (str): the message content
            type (str): one of `debug`, `info`, `warn`, `err`, `fatal`

        Raises:
            ValueError: if the `type` argument is not one of the supported types

        """
        full_msg = '[%s] %s' % (self.node_name, msg)
        # pipe to the right logger
        if type == 'debug':
            rospy.logdebug(full_msg)
        elif type == 'info':
            rospy.loginfo(full_msg)
        elif type == 'warn' or type == 'warning':
            self.set_health(NodeHealth.WARNING, full_msg)
            rospy.logwarn(full_msg)
        elif type == 'err' or type == 'error':
            self.set_health(NodeHealth.ERROR, full_msg)
            rospy.logerr(full_msg)
        elif type == 'fatal':
            self.set_health(NodeHealth.FATAL, full_msg)
            rospy.logfatal(full_msg)
        else:
            raise ValueError('Type argument value %s is not supported!' % type)

    def _srv_switch(self, request):
        """
        Args:
            request (:obj:`std_srvs.srv.SetBool`): The switch request from the `~switch` callback.

        Returns:
            :obj:`std_srvs.srv.SetBoolResponse`: Response for successful feedback

        """
        old_state = self._switch
        self._switch = new_state = request.data
        # propagate switch change to publishers and subscribers
        for pub in self.publishers:
            pub.active = self._switch
        for sub in self.subscribers:
            sub.active = self._switch
        # update node switch in the diagnostics manager
        if DTROSDiagnostics.enabled():
            DTROSDiagnostics.getInstance().update_node(
                enabled=self._switch
            )
        # create a response to the service call
        msg = 'Node switched from [%s] to [%s]' % (
            'on' if old_state else 'off',
            'on' if new_state else 'off'
        )
        # print out the change in state
        self.log(msg)
        # reply to the service call
        response = SetBoolResponse()
        response.success = True
        response.message = msg
        return response

    def _srv_get_params_list(self, request):
        """
        Args:
            request (:obj:`duckietown_msgs.srv.NodeGetParamsList`): Service request message.

        Returns:
            :obj:`duckietown_msgs.srv.NodeGetParamsList`: Parameters list

        """
        return NodeGetParamsListResponse(
            parameters=[
                NodeParameter(
                    node=rospy.get_name(),
                    name=p.name,
                    type=p.type.value,
                    **p.options()
                ) for p in self.parameters
            ]
        )

    def _srv_request_param_update(self, request):
        """
        Args:
            request (:obj:`duckietown_msgs.srv.NodeRequestParamsUpdate`): Service request message.

        Returns:
            :obj:`duckietown_msgs.srv.NodeRequestParamsUpdate`: Success feedback

        """
        try:
            self._parameters[request.parameter].force_update()
            return NodeRequestParamsUpdateResponse(success=True)
        except (KeyError, rospy.exceptions.ROSException):
            return NodeRequestParamsUpdateResponse(success=False)

    def _add_param(self, param):
        if not isinstance(param, DTParam):
            raise ValueError('Expected type duckietown.DTParam, got %s instead' % str(type(param)))
        self._parameters[param] = DTParam

    def _has_param(self, param):
        return param in self._parameters

    def _register_publisher(self, publisher):
        self._publishers.append(publisher)

    def _register_subscriber(self, subscriber):
        self._subscribers.append(subscriber)

    def onShutdown(self):
        """Shutdown procedure."""
        self.is_shutdown = True
        self.log('Shutdown.')