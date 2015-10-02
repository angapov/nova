# Copyright 2012 Grid Dynamics
# Copyright 2013 Inktank Storage, Inc.
# Copyright 2014 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import urllib

try:
    import rados
    import rbd
except ImportError:
    rados = None
    rbd = None

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import units

from nova import exception
from nova.i18n import _
from nova.i18n import _LE
from nova.i18n import _LW
from nova import utils

LOG = logging.getLogger(__name__)


class RBDVolumeProxy(object):
    """Context manager for dealing with an existing rbd volume.

    This handles connecting to rados and opening an ioctx automatically, and
    otherwise acts like a librbd Image object.

    The underlying librados client and ioctx can be accessed as the attributes
    'client' and 'ioctx'.
    """
    def __init__(self, driver, name, pool=None, snapshot=None,
                 read_only=False):
        client, ioctx = driver._connect_to_rados(pool)
        try:
            snap_name = snapshot.encode('utf8') if snapshot else None
            self.volume = rbd.Image(ioctx, name.encode('utf8'),
                                    snapshot=snap_name,
                                    read_only=read_only)
        except rbd.ImageNotFound:
            with excutils.save_and_reraise_exception():
                LOG.debug("rbd image %s does not exist", name)
                driver._disconnect_from_rados(client, ioctx)
        except rbd.Error:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("error opening rbd image %s"), name)
                driver._disconnect_from_rados(client, ioctx)

        self.driver = driver
        self.client = client
        self.ioctx = ioctx

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        try:
            self.volume.close()
        finally:
            self.driver._disconnect_from_rados(self.client, self.ioctx)

    def __getattr__(self, attrib):
        return getattr(self.volume, attrib)


class RADOSClient(object):
    """Context manager to simplify error handling for connecting to ceph."""
    def __init__(self, driver, pool=None):
        self.driver = driver
        self.cluster, self.ioctx = driver._connect_to_rados(pool)

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.driver._disconnect_from_rados(self.cluster, self.ioctx)

    @property
    def features(self):
        features = self.cluster.conf_get('rbd_default_features')
        if ((features is None) or (int(features) == 0)):
            features = rbd.RBD_FEATURE_LAYERING
        return int(features)


class RBDDriver(object):

    def __init__(self, pool, ceph_conf, rbd_user):
        self.pool = pool.encode('utf8')
        # NOTE(angdraug): rados.Rados fails to connect if ceph_conf is None:
        # https://github.com/ceph/ceph/pull/1787
        self.ceph_conf = ceph_conf.encode('utf8') if ceph_conf else ''
        self.rbd_user = rbd_user.encode('utf8') if rbd_user else None
        if rbd is None:
            raise RuntimeError(_('rbd python libraries not found'))

    def _connect_to_rados(self, pool=None):
        client = rados.Rados(rados_id=self.rbd_user,
                                  conffile=self.ceph_conf)
        try:
            client.connect()
            pool_to_open = pool or self.pool
            ioctx = client.open_ioctx(pool_to_open.encode('utf-8'))
            return client, ioctx
        except rados.Error:
            # shutdown cannot raise an exception
            client.shutdown()
            raise

    def _disconnect_from_rados(self, client, ioctx):
        # closing an ioctx cannot raise an exception
        ioctx.close()
        client.shutdown()

    def ceph_args(self):
        """List of command line parameters to be passed to ceph commands to
           reflect RBDDriver configuration such as RBD user name and location
           of ceph.conf.
        """
        args = []
        if self.rbd_user:
            args.extend(['--id', self.rbd_user])
        if self.ceph_conf:
            args.extend(['--conf', self.ceph_conf])
        return args

    def get_mon_addrs(self):
        args = ['ceph', 'mon', 'dump', '--format=json'] + self.ceph_args()
        out, _ = utils.execute(*args)
        lines = out.split('\n')
        if lines[0].startswith('dumped monmap epoch'):
            lines = lines[1:]
        monmap = jsonutils.loads('\n'.join(lines))
        addrs = [mon['addr'] for mon in monmap['mons']]
        hosts = []
        ports = []
        for addr in addrs:
            host_port = addr[:addr.rindex('/')]
            host, port = host_port.rsplit(':', 1)
            hosts.append(host.strip('[]'))
            ports.append(port)
        return hosts, ports

    def parse_url(self, url):
        prefix = 'rbd://'
        if not url.startswith(prefix):
            reason = _('Not stored in rbd')
            raise exception.ImageUnacceptable(image_id=url, reason=reason)
        pieces = map(urllib.unquote, url[len(prefix):].split('/'))
        if '' in pieces:
            reason = _('Blank components')
            raise exception.ImageUnacceptable(image_id=url, reason=reason)
        if len(pieces) != 4:
            reason = _('Not an rbd snapshot')
            raise exception.ImageUnacceptable(image_id=url, reason=reason)
        return pieces

    def _get_fsid(self):
        with RADOSClient(self) as client:
            return client.cluster.get_fsid()

    def is_cloneable(self, image_location, image_meta):
        url = image_location['url']
        try:
            fsid, pool, image, snapshot = self.parse_url(url)
        except exception.ImageUnacceptable as e:
            LOG.debug('not cloneable: %s', e)
            return False

        if self._get_fsid() != fsid:
            reason = '%s is in a different ceph cluster' % url
            LOG.debug(reason)
            return False

        if image_meta.disk_format != 'raw':
            reason = ("rbd image clone requires image format to be "
                      "'raw' but image {0} is '{1}'").format(
                          url, image_meta.disk_format)
            LOG.debug(reason)
            return False

        # check that we can read the image
        try:
            return self.exists(image, pool=pool, snapshot=snapshot)
        except rbd.Error as e:
            LOG.debug('Unable to open image %(loc)s: %(err)s' %
                      dict(loc=url, err=e))
            return False

    def clone(self, image_location, dest_name):
        _fsid, pool, image, snapshot = self.parse_url(
                image_location['url'])
        LOG.debug('cloning %(pool)s/%(img)s@%(snap)s' %
                  dict(pool=pool, img=image, snap=snapshot))
        with RADOSClient(self, str(pool)) as src_client:
            with RADOSClient(self) as dest_client:
                rbd.RBD().clone(src_client.ioctx,
                                     image.encode('utf-8'),
                                     snapshot.encode('utf-8'),
                                     dest_client.ioctx,
                                     dest_name,
                                     features=src_client.features)

    def size(self, name):
        with RBDVolumeProxy(self, name) as vol:
            return vol.size()

    def resize(self, name, size):
        """Resize RBD volume.

        :name: Name of RBD object
        :size: New size in bytes
        """
        LOG.debug('resizing rbd image %s to %d', name, size)
        with RBDVolumeProxy(self, name) as vol:
            vol.resize(size)

    def exists(self, name, pool=None, snapshot=None):
        try:
            with RBDVolumeProxy(self, name,
                                pool=pool,
                                snapshot=snapshot,
                                read_only=True):
                return True
        except rbd.ImageNotFound:
            return False

    def remove_image(self, name):
        """Remove RBD volume

        :name: Name of RBD volume
        """
        with RADOSClient(self, self.pool) as client:
            try:
                rbd.RBD().remove(client.ioctx, name)
            except rbd.ImageNotFound:
                LOG.warn(_LW('image %(volume)s in pool %(pool)s can not be '
                             'found, failed to remove'),
                            {'volume': name, 'pool': self.pool})
            except rbd.ImageHasSnapshots:
                LOG.error(_LE('image %(volume)s in pool %(pool)s has '
                              'snapshots, failed to remove'),
                            {'volume': name, 'pool': self.pool})

    def import_image(self, base, name):
        """Import RBD volume from image file.

        Uses the command line import instead of librbd since rbd import
        command detects zeroes to preserve sparseness in the image.

        :base: Path to image file
        :name: Name of RBD volume
        """
        args = ['--pool', self.pool, base, name]
        # Image format 2 supports cloning,
        # in stable ceph rbd release default is not 2,
        # we need to use it explicitly.
        args += ['--image-format=2']
        args += self.ceph_args()
        utils.execute('rbd', 'import', *args)

    def export_image(self, image_url, dst_path):
        """Export RBD volume to dst_path file.

        Uses 'rbd export' command line tool.

        :image_url: Image URL as an urlparse ParseResult
        :dst_path:  Full path to local file
        """
        pool, image, snapshot = image_url.path[1:].split('/')
        if self.exists(image, pool=pool, snapshot=snapshot):
            args = self.ceph_args()
            args += ['--pool', pool, '--snap', snapshot, image, dst_path]
            LOG.info(_LI('Directly downloading image '
                         '%s@%s using rbd export'), % (image, snapshot))
            try:
                utils.execute('rbd', 'export', *args)
            except Exception as ex:
                LOG.error(_LE('RBD image export failed: %s'), % ex)
                raise

        else:
            LOG.warn(_LW("RBD image %s@%s not found"), % (image, snapshot))
            raise rbd.ImageNotFound

    def cleanup_volumes(self, instance):
        def _cleanup_vol(ioctx, volume, retryctx):
            try:
                rbd.RBD().remove(client.ioctx, volume)
                raise loopingcall.LoopingCallDone(retvalue=False)
            except (rbd.ImageBusy, rbd.ImageHasSnapshots):
                LOG.warn(_LW('rbd remove %(volume)s in pool %(pool)s '
                             'failed'),
                         {'volume': volume, 'pool': self.pool})
            retryctx['retries'] -= 1
            if retryctx['retries'] <= 0:
                raise loopingcall.LoopingCallDone()

        with RADOSClient(self, self.pool) as client:

            def belongs_to_instance(disk):
                return disk.startswith(instance.uuid)

            volumes = rbd.RBD().list(client.ioctx)
            for volume in filter(belongs_to_instance, volumes):
                # NOTE(danms): We let it go for ten seconds
                retryctx = {'retries': 10}
                timer = loopingcall.FixedIntervalLoopingCall(
                    _cleanup_vol, client.ioctx, volume, retryctx)
                timed_out = timer.start(interval=1).wait()
                if timed_out:
                    # NOTE(danms): Run this again to propagate the error, but
                    # if it succeeds, don't raise the loopingcall exception
                    try:
                        _cleanup_vol(client.ioctx, volume, retryctx)
                    except loopingcall.LoopingCallDone:
                        pass

    def get_pool_info(self):
        with RADOSClient(self) as client:
            stats = client.cluster.get_cluster_stats()
            return {'total': stats['kb'] * units.Ki,
                    'free': stats['kb_avail'] * units.Ki,
                    'used': stats['kb_used'] * units.Ki}
