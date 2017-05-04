"""Convert scikit-learn estimators into an OpenMLFlows and vice versa."""

import copy
from collections import OrderedDict
from distutils.version import LooseVersion
import importlib
import json
import json.decoder
import re
import warnings
import sys
from openml.flows import type_check

import numpy as np
import scipy.stats.distributions
import sklearn.base
import sklearn.model_selection
# Necessary to have signature available in python 2.7
from sklearn.utils.fixes import signature

from openml.flows import OpenMLFlow
from openml.exceptions import PyOpenMLError

if sys.version_info >= (3, 5):
    from json.decoder import JSONDecodeError
else:
    JSONDecodeError = ValueError

DEPENDENCIES_PATTERN = re.compile(
    '^(?P<name>[\w\-]+)((?P<operation>==|>=|>)(?P<version>(\d+\.)?(\d+\.)?(\d+)))?$')


def sklearn_to_flow(o, parent_model=None):
    # A primitive parameter
    if type_check.is_primitive_parameter(o):
        return o

    # The main model or a sub model
    if type_check.is_estimator(o):
        return _serialize_model(o)

    # A list-like object
    if type_check.is_list_like(o):
        return _serialize_list(o, parent_model)

    # A dictionary
    if type_check.is_dict(o):
        return _serialize_dict(o, parent_model)

    # A type
    if type_check.is_type(o):
        return _serialize_type(o)

    # A scipy random variable
    if type_check.is_random_variable(o):
        return _serialize_random_variable(o)

    # A function
    if type_check.is_function(o):
        return _serialize_function(o)

    # A cross-validator
    if type_check.is_cross_validator(o):
        return _serialize_cross_validator(o)

    # Object does not have the right type
    raise TypeError(o, type(o))


def flow_to_sklearn(o, **kwargs):
    # First, we need to check whether the presented object is a json string.
    # JSON strings are used to encoder parameter values. By passing around
    # json strings for parameters, we make sure that we can flow_to_sklearn
    # the parameter values to the correct type.
    if type_check.is_string(o):
        try:
            o = json.loads(o)
        except JSONDecodeError:
            pass

    if type_check.is_dict(o):
        # Check if the dict encodes a 'special' object, which could not
        # easily converted into a string, but rather the information to
        # re-create the object were stored in a dictionary.
        if 'oml-python:serialized_object' in o:
            serialized_type = o['oml-python:serialized_object']
            value = o['value']
            if serialized_type == 'type':
                rval = _deserialize_type(value, **kwargs)
            elif serialized_type == 'rv_frozen':
                rval = _deserialize_rv_frozen(value, **kwargs)
            elif serialized_type == 'function':
                rval = _deserialize_function(value, **kwargs)
            elif serialized_type == 'component_reference':
                value = flow_to_sklearn(value)
                step_name = value['step_name']
                key = value['key']
                component = flow_to_sklearn(kwargs['components'][key])
                # The component is now added to where it should be used
                # later. It should not be passed to the constructor of the
                # main flow object.
                del kwargs['components'][key]
                if step_name is None:
                    rval = component
                else:
                    rval = (step_name, component)
            elif serialized_type == 'cv_object':
                rval = _deserialize_cross_validator(value, **kwargs)
            else:
                raise ValueError('Cannot flow_to_sklearn %s' % serialized_type)

        else:
            rval = OrderedDict((flow_to_sklearn(key, **kwargs),
                                flow_to_sklearn(value, **kwargs))
                               for key, value in sorted(o.items()))
    elif isinstance(o, (list, tuple)):
        rval = [flow_to_sklearn(element, **kwargs) for element in o]
        if isinstance(o, tuple):
            rval = tuple(rval)
    elif type_check.is_primitive_parameter(o):
        rval = o
    elif isinstance(o, OpenMLFlow):
        rval = _deserialize_model(o, **kwargs)
    else:
        raise TypeError(o)

    return rval


def _serialize_model(model):
    """Create an OpenMLFlow.

    Calls `sklearn_to_flow` recursively to properly serialize the
    parameters to strings and the components (other models) to OpenMLFlows.

    Parameters
    ----------
    model : sklearn estimator

    Returns
    -------
    OpenMLFlow

    """

    # Get all necessary information about the model objects itself
    parameters, parameters_meta_info, sub_components, sub_components_explicit = \
        _extract_information_from_model(model)

    # Check that a component does not occur multiple times in a flow as this
    # is not supported by OpenML
    _check_multiple_occurence_of_component_in_flow(model, sub_components)

    # Create a flow name, which contains all components in brackets, for example RandomizedSearchCV(Pipeline(
    # StandardScaler,AdaBoostClassifier(DecisionTreeClassifier)),StandardScaler,AdaBoostClassifier(
    # DecisionTreeClassifier))
    class_name = model.__module__ + "." + model.__class__.__name__

    # will be part of the name (in brackets)
    sub_components_names = ""
    for key in sub_components:
        if key in sub_components_explicit:
            sub_components_names += "," + key + "=" + sub_components[key].name
        else:
            sub_components_names += "," + sub_components[key].name

    if sub_components_names:
        # slice operation on string in order to get rid of leading comma
        name = '%s(%s)' % (class_name, sub_components_names[1:])
    else:
        name = class_name

    # Get the external versions of all sub-components
    external_version = _get_external_version_string(model, sub_components)

    dependencies = [_format_external_version('sklearn', sklearn.__version__),
                    'numpy>=1.6.1', 'scipy>=0.9']
    dependencies = '\n'.join(dependencies)

    flow = OpenMLFlow(name=name,
                      class_name=class_name,
                      description='Automatically created scikit-learn flow.',
                      model=model,
                      components=sub_components,
                      parameters=parameters,
                      parameters_meta_info=parameters_meta_info,
                      external_version=external_version,
                      tags=[],
                      language='English',
                      # TODO fill in dependencies!
                      dependencies=dependencies)

    return flow


def _get_external_version_string(model, sub_components):
    # Create external version string for a flow, given the model and the
    # already parsed dictionary of sub_components. Retrieves the external
    # version of all subcomponents, which themselves already contain all
    # requirements for their subcomponents. The external version string is a
    # sorted concatenation of all modules which are present in this run.
    model_package_name = model.__module__.split('.')[0]
    module = importlib.import_module(model_package_name)
    model_package_version_number = module.__version__
    external_version = _format_external_version(model_package_name,
                                                model_package_version_number)
    external_versions = set()
    external_versions.add(external_version)
    for visitee in sub_components.values():
        for external_version in visitee.external_version.split(','):
            external_versions.add(external_version)
    external_versions = list(sorted(external_versions))
    external_version = ','.join(external_versions)
    return external_version


def _check_multiple_occurence_of_component_in_flow(model, sub_components):
    to_visit_stack = []
    to_visit_stack.extend(sub_components.values())
    known_sub_components = set()
    while len(to_visit_stack) > 0:
        visitee = to_visit_stack.pop()
        if visitee.name in known_sub_components:
            raise ValueError('Found a second occurence of component %s when '
                             'trying to serialize %s.' % (visitee.name, model))
        else:
            known_sub_components.add(visitee.name)
            to_visit_stack.extend(visitee.components.values())


def _extract_information_from_model(model):
    # This function contains four "global" states and is quite long and
    # complicated. If it gets to complicated to ensure it's correctness,
    # it would be best to make it a class with the four "global" states being
    # the class attributes and the if/elif/else in the for-loop calls to
    # separate class methods

    # Stores all entities that should become subcomponents
    sub_components = OrderedDict()

    # Stores the keys of all subcomponents that should become
    sub_components_explicit = set()

    parameters = OrderedDict()
    parameters_meta_info = OrderedDict()

    model_parameters = model.get_params(deep=False)
    for k, v in sorted(model_parameters.items(), key=lambda t: t[0]):
        rval = sklearn_to_flow(v, model)

        # Check if rval is a homogeneous list-like object of lists or tuples
        if type_check.is_homogeneous_list(rval, types=(list, tuple)):

            # Steps in a pipeline or feature union, or base classifiers in voting classifier
            parameter_value = list()
            reserved_keywords = set(model.get_params(deep=False).keys())

            for sub_component_tuple in rval:
                identifier, sub_component = sub_component_tuple
                sub_component_type = type(sub_component_tuple)

                if identifier in reserved_keywords:
                    parent_model_name = model.__module__ + "." + \
                                        model.__class__.__name__
                    raise PyOpenMLError('Found element shadowing official ' + \
                                        'parameter for %s: %s' % (parent_model_name, identifier))

                if sub_component is None:
                    # In a FeatureUnion it is legal to have a None step

                    pv = [identifier, None]
                    if sub_component_type is tuple:
                        pv = tuple(pv)
                    parameter_value.append(pv)

                else:
                    # Add the component to the list of components, add a
                    # component reference as a placeholder to the list of
                    # parameters, which will be replaced by the real component
                    # when deserializing the parameter
                    sub_components[identifier] = sub_component
                    sub_components_explicit.add(identifier)
                    component_reference = _make_component_reference(identifier, identifier, model)
                    parameter_value.append(component_reference)

            if isinstance(rval, tuple):
                parameter_value = tuple(parameter_value)

            # Here (and in the elif and else branch below) are the only
            # places where we encode a value as json to make sure that all
            # parameter values still have the same type after
            # deserialization
            parameter_value = json.dumps(parameter_value)
            parameters[k] = parameter_value

        elif isinstance(rval, OpenMLFlow):

            # A subcomponent, for example the base model in AdaBoostClassifier
            sub_components[k] = rval
            sub_components_explicit.add(k)
            component_reference = _make_component_reference(k, None, model)
            parameters[k] = json.dumps(component_reference)

        else:

            # a regular hyperparameter
            if not (hasattr(rval, '__len__') and len(rval) == 0):
                rval = json.dumps(rval)
                parameters[k] = rval
            else:
                parameters[k] = None

        parameters_meta_info[k] = OrderedDict((('description', None),
                                               ('data_type', None)))

    return parameters, parameters_meta_info, sub_components, sub_components_explicit


def _deserialize_model(flow, **kwargs):
    model_name = flow.class_name
    _check_dependencies(flow.dependencies)

    parameters = flow.parameters
    components = flow.components
    parameter_dict = OrderedDict()

    # Do a shallow copy of the components dictionary so we can remove the
    # components from this copy once we added them into the pipeline. This
    # allows us to not consider them any more when looping over the
    # components, but keeping the dictionary of components untouched in the
    # original components dictionary.
    components_ = copy.copy(components)

    for name in parameters:
        value = parameters.get(name)
        rval = flow_to_sklearn(value, components=components_)
        parameter_dict[name] = rval

    for name in components:
        if name in parameter_dict:
            continue
        if name not in components_:
            continue
        value = components[name]
        rval = flow_to_sklearn(value)
        parameter_dict[name] = rval

    module_name = model_name.rsplit('.', 1)
    try:
        model_class = getattr(importlib.import_module(module_name[0]),
                              module_name[1])
    except:
        warnings.warn('Cannot create model %s for flow.' % model_name)
        return None

    return model_class(**parameter_dict)


def _check_dependencies(dependencies):
    if not dependencies:
        return

    dependencies = dependencies.split('\n')
    for dependency_string in dependencies:
        match = DEPENDENCIES_PATTERN.match(dependency_string)
        dependency_name = match.group('name')
        operation = match.group('operation')
        version = match.group('version')

        module = importlib.import_module(dependency_name)
        required_version = LooseVersion(version)
        installed_version = LooseVersion(module.__version__)

        if operation == '==':
            check = required_version == installed_version
        elif operation == '>':
            check = installed_version > required_version
        elif operation == '>=':
            check = installed_version > required_version or \
                    installed_version == required_version
        else:
            raise NotImplementedError(
                'operation \'%s\' is not supported' % operation)
        if not check:
            raise ValueError('Trying to deserialize a model with dependency '
                             '%s not satisfied.' % dependency_string)


def _make_component_reference(key, step_name, model):
    cr_value = OrderedDict([
        ('key', key),
        ('step_name', step_name)
    ])

    component_reference = _make_serialized_object('component_reference', cr_value)
    component_reference['value'] = cr_value
    component_reference = sklearn_to_flow(component_reference, model)
    return component_reference


def _make_serialized_object(object_name, value):
    return OrderedDict([
        ('oml-python:serialized_object', object_name),
        ('value', value)
    ])


def _serialize_list(o, parent_model):
    rval = [sklearn_to_flow(element, parent_model) for element in o]
    if isinstance(o, tuple):
        rval = tuple(rval)
    return rval


def _serialize_dict(o, parent_model):
    # Convert to OrderedDict
    if not isinstance(o, OrderedDict):
        o = OrderedDict([(key, value) for key, value in sorted(o.items())])

    # Convert each key and value to flow
    serialized_dict = OrderedDict()
    for key, value in o.items():
        if not type_check.is_string(key):
            raise TypeError('Can only use string as keys, you passed type %s for value %s.' % (type(key), str(key)))
        key = sklearn_to_flow(key, parent_model)
        value = sklearn_to_flow(value, parent_model)
        serialized_dict[key] = value

    # Return the serialized dictionary
    return serialized_dict


def _serialize_type(o):
    mapping = {float: 'float',
               np.float: 'np.float',
               np.float32: 'np.float32',
               np.float64: 'np.float64',
               int: 'int',
               np.int: 'np.int',
               np.int32: 'np.int32',
               np.int64: 'np.int64'}
    return _make_serialized_object('type', mapping[o])


def _deserialize_type(o, **kwargs):
    mapping = {'float': float,
               'np.float': np.float,
               'np.float32': np.float32,
               'np.float64': np.float64,
               'int': int,
               'np.int': np.int,
               'np.int32': np.int32,
               'np.int64': np.int64}
    return mapping[o]


def _serialize_random_variable(o):
    return _make_serialized_object('rv_frozen', OrderedDict([
        ('dist', o.dist.__class__.__module__ + '.' + o.dist.__class__.__name__),
        ('a', o.a),
        ('b', o.b),
        ('args', o.args),
        ('kwds', o.kwds)
    ]))


def _serialize_function(o):
    return _make_serialized_object('function', o.__module__ + '.' + o.__name__)


def _deserialize_rv_frozen(o, **kwargs):
    args = o['args']
    kwds = o['kwds']
    a = o['a']
    b = o['b']
    dist_name = o['dist']

    module_name = dist_name.rsplit('.', 1)
    try:
        rv_class = getattr(importlib.import_module(module_name[0]),
                           module_name[1])
    except:
        warnings.warn('Cannot create model %s for flow.' % dist_name)
        return None

    dist = scipy.stats.distributions.rv_frozen(rv_class(), *args, **kwds)
    dist.a = a
    dist.b = b

    return dist


def _deserialize_function(name, **kwargs):
    module_name = name.rsplit('.', 1)
    try:
        function_handle = getattr(importlib.import_module(module_name[0]),
                                  module_name[1])
    except Exception as e:
        warnings.warn('Cannot load function %s due to %s.' % (name, e))
        return None
    return function_handle


def _serialize_cross_validator(o):
    parameters = OrderedDict()

    # XXX this is copied from sklearn.model_selection._split
    cls = o.__class__
    init = getattr(cls.__init__, 'deprecated_original', cls.__init__)

    # Ignore varargs, kw and default values and pop self
    init_signature = signature(init)

    # Consider the constructor parameters excluding 'self'
    if init is object.__init__:
        args = []
    else:
        args = sorted([p.name for p in init_signature.parameters.values()
                       if p.name != 'self' and p.kind != p.VAR_KEYWORD])

    for key in args:
        # We need deprecation warnings to always be on in order to
        # catch deprecated param values.
        # This is set in utils/__init__.py but it gets overwritten
        # when running under python3 somehow.
        warnings.simplefilter("always", DeprecationWarning)
        try:
            with warnings.catch_warnings(record=True) as w:
                value = getattr(o, key, None)
            if len(w) and w[0].category == DeprecationWarning:
                # if the parameter is deprecated, don't show it
                continue
        finally:
            warnings.filters.pop(0)

        if not (hasattr(value, '__len__') and len(value) == 0):
            value = json.dumps(value)
            parameters[key] = value
        else:
            parameters[key] = None

    return _make_serialized_object('cv_object', OrderedDict([
        ('name', o.__module__ + "." + o.__class__.__name__),
        ('parameters', parameters)
    ]))


def _check_n_jobs(model):
    '''
    Returns True if the parameter settings of model are chosen s.t. the model
     will run on a single core (in that case, openml-python can measure runtimes)
    '''

    def check(param_dict, disallow_parameter=False):
        for param, value in param_dict.items():
            # n_jobs is scikitlearn parameter for paralizing jobs
            if param.split('__')[-1] == 'n_jobs':
                # 0 = illegal value (?), 1 = use one core,  n = use n cores
                # -1 = use all available cores -> this makes it hard to
                # measure runtime in a fair way
                if value != 1 or disallow_parameter:
                    return False
        return True

    if not (isinstance(model, sklearn.base.BaseEstimator) or
                isinstance(model, sklearn.model_selection._search.BaseSearchCV)):
        raise ValueError('model should be BaseEstimator or BaseSearchCV')

    # make sure that n_jobs is not in the parameter grid of optimization procedure
    if isinstance(model, sklearn.model_selection._search.BaseSearchCV):
        if isinstance(model, sklearn.model_selection.GridSearchCV):
            param_distributions = model.param_grid
        elif isinstance(model, sklearn.model_selection.RandomizedSearchCV):
            param_distributions = model.param_distributions
        else:
            print('Warning! Using subclass BaseSearchCV other than ' \
                  '{GridSearchCV, RandomizedSearchCV}. Should implement param check. ')

            # Return false if we can't determine the param_distributions
            return False

        if not check(param_distributions, True):
            raise PyOpenMLError('openml-python should not be used to '
                                'optimize the n_jobs parameter.')

    # check the parameters for n_jobs
    return check(model.get_params(), False)


def _deserialize_cross_validator(value, **kwargs):
    model_name = value['name']
    parameters = value['parameters']

    module_name = model_name.rsplit('.', 1)
    model_class = getattr(importlib.import_module(module_name[0]),
                          module_name[1])
    for parameter in parameters:
        parameters[parameter] = flow_to_sklearn(parameters[parameter])
    return model_class(**parameters)


def _format_external_version(model_package_name, model_package_version_number):
    return '%s==%s' % (model_package_name, model_package_version_number)