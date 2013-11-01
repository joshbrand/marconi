# Copyright (c) 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo.config import cfg
import six

from marconi.common import decorators
from marconi.common.storage import select
from marconi.common import utils as common_utils
from marconi.queues import storage
from marconi.queues.storage import errors
from marconi.queues.storage import utils

_CATALOG_OPTIONS = [
    cfg.IntOpt('storage', default='sqlite',
               help='Catalog storage driver'),
]

_CATALOG_GROUP = 'queues:sharding:catalog'


class DataDriver(storage.DataDriverBase):
    """Sharding meta-driver for routing requests to multiple backends.

    :param storage_conf: Ignored, since this is a meta-driver
    :param catalog_conf: Options pertaining to the shard catalog
    """

    def __init__(self, conf, control):
        super(DataDriver, self).__init__(conf)
        self._shard_catalog = Catalog(conf, control)

    @decorators.lazy_property(write=False)
    def queue_controller(self):
        return QueueController(self._shard_catalog)

    @decorators.lazy_property(write=False)
    def message_controller(self):
        return MessageController(self._shard_catalog)

    @decorators.lazy_property(write=False)
    def claim_controller(self):
        return ClaimController(self._shard_catalog)


class RoutingController(storage.base.ControllerBase):
    """Routes operations to the appropriate shard.

    This controller stands in for a regular storage controller,
    routing operations to a driver instance that represents
    the shard to which the queue has been assigned.

    Do not instantiate this class directly; use one of the
    more specific child classes instead.
    """

    _resource_name = None

    def __init__(self, shard_catalog):
        super(RoutingController, self).__init__(None)
        self._ctrl_property_name = self._resource_name + '_controller'
        self._shard_catalog = shard_catalog

    @decorators.cached_getattr
    def __getattr__(self, name):
        # NOTE(kgriffs): Use a closure trick to avoid
        # some attr lookups each time forward() is called.
        lookup = self._shard_catalog.lookup

        # NOTE(kgriffs): Assume that every controller method
        # that is exposed to the transport declares queue name
        # as its first arg. The only exception to this
        # is QueueController.list
        def forward(queue, *args, **kwargs):
            # NOTE(kgriffs): Using .get since 'project' is an
            # optional argument.
            storage = lookup(queue, kwargs.get('project'))
            target_ctrl = getattr(storage, self._ctrl_property_name)
            return getattr(target_ctrl, name)(queue, *args, **kwargs)

        return forward


class QueueController(RoutingController):
    """Controller to facilitate special processing for queue operations.
    """

    _resource_name = 'queue'

    def __init__(self, shard_catalog):
        super(QueueController, self).__init__(shard_catalog)
        self._lookup = self._shard_catalog.lookup

    def list(self, project=None, marker=None,
             limit=10, detailed=False):
        # TODO(cpp-cabrera): fill in sharded list queues
        # implementation.

        return []

    def create(self, name, project=None):
        self._shard_catalog.register(name, project)

        # NOTE(cpp-cabrera): This should always succeed since we just
        # registered the project/queue. There is a race condition,
        # however. If between the time we register a queue and go to
        # look it up, the queue is deleted, then this assertion will
        # fail.
        target = self._lookup(name, project)
        assert target, 'Failed to register queue'

        return target.queue_controller.create(name, project)

    def delete(self, name, project=None):
        # NOTE(cpp-cabrera): If we fail to find a project/queue in the
        # catalogue for a delete, just ignore it.
        target = self._lookup(name, project)
        if target:

            # NOTE(cpp-cabrera): Now we found the controller. First,
            # attempt to delete it from storage. IFF the deletion is
            # successful, then remove it from the catalogue.
            control = target.queue_controller
            ret = control.delete(name, project)
            self._shard_catalog.deregister(name, project)
            return ret

        return None

    def exists(self, name, project=None):
        target = self._lookup(name, project)
        if target:
            control = target.queue_controller
            return control.exists(name, project=project)
        return False

    def get_metadata(self, name, project=None):
        target = self._lookup(name, project)
        if target:
            control = target.queue_controller
            return control.get_metadata(name, project=project)
        raise errors.QueueDoesNotExist(name, project)

    def set_metadata(self, name, metadata, project=None):
        target = self._lookup(name, project)
        if target:
            control = target.queue_controller
            return control.set_metadata(name, metadata=metadata,
                                        project=project)
        raise errors.QueueDoesNotExist(name, project)

    def stats(self, name, project=None):
        target = self._lookup(name, project)
        if target:
            control = target.queue_controller
            return control.stats(name, project=project)
        raise errors.QueueDoesNotExist(name, project)


class MessageController(RoutingController):
    _resource_name = 'message'

    def __init__(self, shard_catalog):
        super(MessageController, self).__init__(shard_catalog)
        self._lookup = self._shard_catalog.lookup

    def post(self, queue, project, messages, client_uuid):
        target = self._lookup(queue, project)
        if target:
            control = target.message_controller
            return control.post(queue, project=project,
                                messages=messages,
                                client_uuid=client_uuid)
        raise errors.QueueDoesNotExist(project, queue)

    def delete(self, queue, project, message_id, claim):
        target = self._lookup(queue, project)
        if target:
            control = target.message_controller
            return control.delete(queue, project=project,
                                  message_id=message_id, claim=claim)
        return None

    def bulk_delete(self, queue, project, message_ids):
        target = self._lookup(queue, project)
        if target:
            control = target.message_controller
            return control.bulk_delete(queue, project=project,
                                       message_ids=message_ids)
        return None

    def bulk_get(self, queue, project, message_ids):
        target = self._lookup(queue, project)
        if target:
            control = target.message_controller
            return control.bulk_get(queue, project=project,
                                    message_ids=message_ids)
        return []

    def list(self, queue, project, marker=None, limit=10, echo=False,
             client_uuid=None, include_claimed=False):
        target = self._lookup(queue, project)
        if target:
            control = target.message_controller
            return control.list(queue, project=project,
                                marker=marker, limit=limit,
                                echo=echo, client_uuid=client_uuid,
                                include_claimed=include_claimed)
        return iter([[]])

    def get(self, queue, message_id, project):
        target = self._lookup(queue, project)
        if target:
            control = target.message_controller
            return control.get(queue, message_id=message_id,
                               project=project)
        raise errors.QueueDoesNotExist(project, queue)


class ClaimController(RoutingController):
    _resource_name = 'claim'

    def __init__(self, shard_catalog):
        super(ClaimController, self).__init__(shard_catalog)
        self._lookup = self._shard_catalog.lookup

    def create(self, queue, metadata, project=None, limit=None):
        target = self._lookup(queue, project)
        if target:
            control = target.claim_controller
            return control.create(queue, metadata=metadata,
                                  project=project, limit=limit)
        return [None, []]

    def get(self, queue, claim_id, project):
        target = self._lookup(queue, project)
        if target:
            control = target.claim_controller
            return control.get(queue, claim_id=claim_id,
                               project=project)
        raise errors.ClaimDoesNotExist(claim_id, queue, project)

    def update(self, queue, claim_id, metadata, project):
        target = self._lookup(queue, project)
        if target:
            control = target.claim_controller
            return control.update(queue, claim_id=claim_id,
                                  project=project, metadata=metadata)
        raise errors.ClaimDoesNotExist(claim_id, queue, project)

    def delete(self, queue, claim_id, project):
        target = self._lookup(queue, project)
        if target:
            control = target.claim_controller
            return control.delete(queue, claim_id=claim_id,
                                  project=project)
        return None


class Catalog(object):
    """Represents the mapping between queues and shard drivers."""

    def __init__(self, conf, control):
        self._drivers = {}
        self._conf = conf

        self._conf.register_opts(_CATALOG_OPTIONS, group=_CATALOG_GROUP)
        self._catalog_conf = self._conf[_CATALOG_GROUP]
        self._shards_ctrl = control.shards_controller
        self._catalogue_ctrl = control.catalogue_controller

    def _init_driver(self, shard_id):
        """Given a shard name, returns a storage driver.

        :param shard_id: The name of a shard.
        :type shard_id: six.text_type
        :returns: a storage driver
        :rtype: marconi.queues.storage.base.DataDriver
        """
        shard = self._shards_ctrl.get(shard_id, detailed=True)

        # NOTE(cpp-cabrera): make it *very* clear to data storage
        # drivers that we are operating in sharding mode.
        general_dict_opts = {'dynamic': True}
        general_opts = common_utils.dict_to_conf(general_dict_opts)

        # NOTE(cpp-cabrera): parse general opts: 'queues:drivers'
        uri = shard['uri']
        storage_type = six.moves.urllib_parse.urlparse(uri).scheme
        driver_dict_opts = {'storage': storage_type}
        driver_opts = common_utils.dict_to_conf(driver_dict_opts)

        # NOTE(cpp-cabrera): parse storage-specific opts:
        # 'queues:drivers:storage:{type}'
        storage_dict_opts = shard['options']
        storage_dict_opts['uri'] = shard['uri']
        storage_opts = common_utils.dict_to_conf(storage_dict_opts)
        storage_group = u'queues:drivers:storage:%s' % storage_type

        # NOTE(cpp-cabrera): register those options!
        conf = cfg.ConfigOpts()
        conf.register_opts(general_opts)
        conf.register_opts(driver_opts, group=u'queues:drivers')
        conf.register_opts(storage_opts, group=storage_group)
        return utils.load_storage_driver(conf)

    def register(self, queue, project=None):
        """Register a new queue in the shard catalog.

        This method should be called whenever a new queue is being
        created, and will create an entry in the shard catalog for
        the given queue.

        After using this method to register the queue in the
        catalog, the caller should call `lookup()` to get a reference
        to a storage driver which will allow interacting with the
        queue's assigned backend shard.

        :param queue: Name of the new queue to assign to a shard
        :type queue: six.text_type
        :param project: Project to which the queue belongs, or
            None for the "global" or "generic" project.
        :type project: six.text_type
        :raises: NoShardFound
        """
        if not self._catalogue_ctrl.exists(project, queue):
            # NOTE(cpp-cabrera): limit=0 implies unlimited - select from
            # all shards
            shard = select.weighted(self._shards_ctrl.list(limit=0))
            if not shard:
                raise errors.NoShardFound()
            self._catalogue_ctrl.insert(project, queue, shard['name'])

    def deregister(self, queue, project=None):
        """Removes a queue from the shard catalog.

        Call this method after successfully deleting it from a
        backend shard.

        :param queue: Name of the new queue to assign to a shard
        :type queue: six.text_type
        :param project: Project to which the queue belongs, or
            None for the "global" or "generic" project.
        :type project: six.text_type
        """
        # TODO(cpp-cabrera): invalidate cache here
        self._catalogue_ctrl.delete(project, queue)

    def lookup(self, queue, project=None):
        """Lookup a shard driver for the given queue and project.

        :param queue: Name of the queue for which to find a shard
        :param project: Project to which the queue belongs, or
            None to specify the "global" or "generic" project.

        :returns: A storage driver instance for the appropriate shard. If
            the driver does not exist yet, it is created and cached.
        :rtype: Maybe DataDriver
        """

        # TODO(cpp-cabrera): add caching lookup here
        try:
            shard_id = self._catalogue_ctrl.get(project, queue)['shard']
        except errors.QueueNotMapped:
            return None

        # NOTE(cpp-cabrera): cache storage driver connection
        try:
            driver = self._drivers[shard_id]
        except KeyError:
            self._drivers[shard_id] = driver = self._init_driver(shard_id)

        return driver
