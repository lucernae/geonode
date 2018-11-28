# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
from __future__ import absolute_import

import logging
import os
import shutil
import requests

from django.conf import settings
from django.contrib.gis.gdal import (
    SRSException, DataSource, CoordTransform,
    SpatialReference, OGRGeometry, GDALRaster)
from django.core.urlresolvers import reverse
from django.db.models import signals
from django.dispatch import Signal
from requests.compat import urljoin

from geonode import qgis_server
from geonode.base.models import Link
from geonode.layers.models import Layer, LayerFile
from geonode.layers.utils import is_vector, is_raster
from geonode.maps.models import Map, MapLayer
from geonode.qgis_server.gis_tools import set_attributes
from geonode.qgis_server.helpers import (
    tile_url_format,
    create_qgis_project,
    style_list,
    style_add_url,
    style_set_default_url)
from geonode.qgis_server.models import QGISServerLayer, QGISServerMap
from geonode.qgis_server.qlr import is_qlr, QLRFile, populate_info_from_qlr
from geonode.qgis_server.tasks.update import create_qgis_server_thumbnail
from geonode.qgis_server.xml_utilities import update_xml
from geonode.utils import check_ogc_backend

logger = logging.getLogger("geonode.qgis_server.signals")

if check_ogc_backend(qgis_server.BACKEND_PACKAGE):
    QGIS_layer_directory = settings.QGIS_SERVER_CONFIG['layer_directory']

qgis_map_with_layers = Signal(providing_args=[])


def qgis_server_layer_post_delete(instance, sender, **kwargs):
    """Removes the layer from Local Storage."""
    logger.debug('QGIS Server Layer Post Delete')
    instance.delete_qgis_layer()


def qgis_server_pre_delete(instance, sender, **kwargs):
    """Removes the layer from Local Storage."""
    # logger.debug('QGIS Server Pre Delete')
    # Deleting QGISServerLayer object happened on geonode level since it's
    # OneToOne relationship
    # Delete file is included when deleting the object.


def qgis_server_pre_save(instance, sender, **kwargs):
    """Send information to QGIS Server.

       The attributes sent include:

        * Title
        * Abstract
        * Name
        * Keywords
        * Metadata Links,
        * Point of Contact name and url
    """
    # logger.debug('QGIS Server Pre Save')


def qgis_server_post_save(instance, sender, **kwargs):
    """Save keywords to QGIS Server.

    The way keywords are implemented requires the layer to be saved to the
    database before accessing them.

    This hook also creates QGIS Project. Which is essentials for QGIS Server.
    There are also several Geonode Links generated, like thumbnail and legends

    :param instance: geonode Layer
    :type instance: Layer

    :param sender: geonode Layer type
    :type sender: type(Layer)
    """
    if not sender == Layer:
        return
    # TODO
    # 1. Create or update associated QGISServerLayer [Done]
    # 2. Create Link for the tile and legend.
    logger.debug('QGIS Server Post Save')

    # copy layer to QGIS Layer Directory
    try:
        geonode_layer_path = instance.get_base_file()[0].file.path
    except AttributeError:
        logger.debug('Layer does not have base file')
        return

    qgis_layer, created = QGISServerLayer.objects.get_or_create(
        layer=instance)
    logger.debug('Geonode Layer Path %s' % geonode_layer_path)

    base_filename, original_ext = os.path.splitext(geonode_layer_path)
    extensions = QGISServerLayer.accepted_format

    is_qlr_layer = is_qlr(geonode_layer_path)
    is_vector_layer = is_vector(geonode_layer_path)
    is_raster_layer = is_raster(geonode_layer_path)

    # iterate all extension supported by QGIS
    for ext in extensions:
        if os.path.exists(base_filename + '.' + ext):
            try:
                if created:
                    # Assuming different layer has different filename because
                    # geonode rename it
                    shutil.copy2(
                        base_filename + '.' + ext,
                        QGIS_layer_directory
                    )
                    logger.debug('Create new basefile')
                else:
                    # If there is already a file, replace the old one
                    qgis_layer_base_filename = qgis_layer.qgis_layer_path_prefix
                    shutil.copy2(
                        base_filename + '.' + ext,
                        qgis_layer_base_filename + '.' + ext
                    )
                    logger.debug('Overwrite existing basefile')
                logger.debug(
                    'Copying %s' % base_filename + '.' + ext + ' Success')
                logger.debug('Into %s' % QGIS_layer_directory)
            except IOError as e:
                logger.debug(
                    'Copying %s' % base_filename + '.' + ext + ' FAILED ' + e)
    if created:
        # Only set when creating new QGISServerLayer Object
        geonode_filename = os.path.basename(geonode_layer_path)
        basename = os.path.splitext(geonode_filename)[0]
        qgis_layer.base_layer_path = os.path.join(
            QGIS_layer_directory,
            basename + original_ext  # Already with dot
        )
    qgis_layer.save()

    # refresh to get QGIS Layer
    instance.refresh_from_db()

    # Transform bounds to EPSG:4326 to be used by leaflet.
    skip_bound_transform = False
    try:
        srid_string = None
        if is_vector_layer and not is_qlr_layer:
            # Try to get bbox again but handle exceptions
            datasource = DataSource(geonode_layer_path)
            layer = datasource[0]
            srs = layer.srs
            srs.identify_epsg()
            srid_string = 'EPSG:{0}'.format(srs.srid)
        elif is_raster_layer and not is_qlr_layer:
            rst = GDALRaster(geonode_layer_path)
            srs = rst.srs
            srs.identify_epsg()
            srid_string = 'EPSG:{0}'.format(srs.srid)

        if srid_string == instance.srid:
            # We have a proper srid here
            skip_bound_transform = True
    except SRSException as e:
        # GDAL can't find matching EPSG code
        # We will let QGIS handle on the fly projection
        # However, we need bounds in EPSG:4326 to display in leaflet
        logger.exception(e)
    except Exception as e:
        logger.debug("Can't retrieve projection: {layer}".format(
            layer=geonode_layer_path))
        logger.exception(e)
        # Can not transform bounds if we don't have projection
        skip_bound_transform = True

    # Check QLR
    if is_qlr_layer:
        qlr_obj = QLRFile(geonode_layer_path)
        instance, update_kwargs = populate_info_from_qlr(instance, qlr_obj)
        Layer.objects.filter(id=instance.id).update(**update_kwargs)
        instance.refresh_from_db()

        is_vector_layer = qlr_obj.type == 'vector'
        is_raster_layer = qlr_obj.type == 'raster'

        skip_bound_transform = True

    # Transform bounds to EPSG:4326 when we have undefined projection
    if not skip_bound_transform:
        bound_geom = OGRGeometry.from_bbox((
            instance.bbox_x0, instance.bbox_y0,
            instance.bbox_x1, instance.bbox_y1
        ))
        coord_transform = CoordTransform(srs, SpatialReference('EPSG:4326'))
        bound_geom.transform(coord_transform)
        # update bounds info
        Layer.objects.filter(id=instance.id).update(
            srid='EPSG:4326',
            bbox_x0=bound_geom.envelope.min_x,
            bbox_x1=bound_geom.envelope.max_x,
            bbox_y0=bound_geom.envelope.min_y,
            bbox_y1=bound_geom.envelope.max_y)
        instance.refresh_from_db()

    # base url for geonode
    base_url = settings.SITEURL

    # Set Link for Download Raw in Zip File
    zip_download_url = reverse(
        'qgis_server:download-zip', kwargs={'layername': instance.name})
    zip_download_url = urljoin(base_url, zip_download_url)
    logger.debug('zip_download_url: %s' % zip_download_url)
    if is_vector_layer:
        link_name = 'Zipped Vector Files'
        link_mime = 'SHAPE-ZIP'
    else:
        link_name = 'Zipped All Files'
        link_mime = 'ZIP'

    # Zip file
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=link_name,
        defaults=dict(
            extension='zip',
            mime=link_mime,
            url=zip_download_url,
            link_type='data'
        )
    )

    # WMS link layer workspace
    ogc_wms_url = urljoin(
        settings.SITEURL,
        reverse(
            'qgis_server:layer-request', kwargs={'layername': instance.name}))
    ogc_wms_name = 'OGC WMS: %s Service' % instance.workspace
    ogc_wms_link_type = 'OGC:WMS'
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=ogc_wms_name,
        link_type=ogc_wms_link_type,
        defaults=dict(
            extension='html',
            url=ogc_wms_url,
            mime='text/html',
            link_type=ogc_wms_link_type
        )
    )

    # QGS link layer workspace
    ogc_qgs_url = urljoin(
        base_url,
        reverse(
            'qgis_server:download-qgs',
            kwargs={'layername': instance.name}))
    logger.debug('qgs_download_url: %s' % ogc_qgs_url)
    link_name = 'QGIS project file (.qgs)'
    link_mime = 'application/xml'
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=link_name,
        defaults=dict(
            extension='qgs',
            mime=link_mime,
            url=ogc_qgs_url,
            link_type='data'
        )
    )

    if instance.is_vector():
        # WFS link layer workspace
        ogc_wfs_url = urljoin(
            settings.SITEURL,
            reverse(
                'qgis_server:layer-request',
                kwargs={'layername': instance.name}))
        ogc_wfs_name = 'OGC WFS: %s Service' % instance.workspace
        ogc_wfs_link_type = 'OGC:WFS'
        Link.objects.update_or_create(
            resource=instance.resourcebase_ptr,
            name=ogc_wfs_name,
            link_type=ogc_wfs_link_type,
            defaults=dict(
                extension='html',
                url=ogc_wfs_url,
                mime='text/html',
                link_type=ogc_wfs_link_type
            )
        )

    # QLR link layer workspace
    ogc_qlr_url = urljoin(
        base_url,
        reverse(
            'qgis_server:download-qlr',
            kwargs={'layername': instance.name}))
    logger.debug('qlr_download_url: %s' % ogc_qlr_url)
    link_name = 'QGIS layer file (.qlr)'
    link_mime = 'application/xml'
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=link_name,
        defaults=dict(
            extension='qlr',
            mime=link_mime,
            url=ogc_qlr_url,
            link_type='data'
        )
    )

    # if layer has overwrite attribute, then it probably comes from
    # importlayers management command and needs to be overwritten
    overwrite = getattr(instance, 'overwrite', False)

    # default basemap
    try:
        default_tile = settings.LEAFLET_CONFIG['TILES'][0]
        basemap_name = 'basemap'
        basemap_url = default_tile[1]
    except BaseException:
        basemap_name = None
        basemap_url = None

    # Create the QGIS Project
    response = create_qgis_project(
        instance, qgis_layer.qgis_project_path, overwrite=overwrite,
        internal=True, basemap=basemap_url, basemap_name=basemap_name)

    logger.debug('Creating the QGIS Project : %s' % response.url)
    if response.content != 'OK':
        logger.debug('Result : %s' % response.content)

    # Generate style model cache
    try:
        style_list(instance, internal=False)
    except:
        print 'Failed to fetch styles'

    # Remove QML file if necessary
    try:
        qml_file = instance.upload_session.layerfile_set.get(name='qml')
        if not os.path.exists(qml_file.file.path):
            qml_file.delete()
    except LayerFile.DoesNotExist:
        pass

    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name="Tiles",
        defaults=dict(
            url=tile_url_format(instance.name),
            extension='tiles',
            mime='image/png',
            link_type='image'
        )
    )

    if original_ext.split('.')[-1] in QGISServerLayer.geotiff_format:
        # geotiff link
        geotiff_url = reverse(
            'qgis_server:geotiff', kwargs={'layername': instance.name})
        geotiff_url = urljoin(base_url, geotiff_url)
        logger.debug('geotif_url: %s' % geotiff_url)

        Link.objects.update_or_create(
            resource=instance.resourcebase_ptr,
            name="GeoTIFF",
            defaults=dict(
                extension=original_ext.split('.')[-1],
                url=geotiff_url,
                mime='image/tiff',
                link_type='image'
            )
        )

    # Create legend link
    legend_url = reverse(
        'qgis_server:legend',
        kwargs={'layername': instance.name}
    )
    legend_url = urljoin(base_url, legend_url)
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name='Legend',
        defaults=dict(
            extension='png',
            url=legend_url,
            mime='image/png',
            link_type='image',
        )
    )

    # Create thumbnail
    create_qgis_server_thumbnail.delay(
        instance, overwrite=overwrite)

    # Attributes
    set_attributes(instance)

    # Update xml file

    # Read metadata from layer that InaSAFE use.
    # Some are not found: organisation, email, url
    try:
        new_values = {
            'date': instance.date.isoformat(),
            'abstract': instance.abstract,
            'title': instance.title,
            'license': instance.license_verbose,
        }
    except (TypeError, AttributeError):
        new_values = {}
        pass

    # Get the path of the metadata file
    basename, _ = os.path.splitext(qgis_layer.base_layer_path)
    xml_file_path = basename + '.xml'
    if os.path.exists(xml_file_path):
        try:
            update_xml(xml_file_path, new_values)
        except (TypeError, AttributeError):
            pass

    # Also update xml in QGIS Server
    xml_file_path = qgis_layer.qgis_layer_path_prefix + '.xml'
    if os.path.exists(xml_file_path):
        try:
            update_xml(xml_file_path, new_values)
        except (TypeError, AttributeError):
            pass

    # Remove existing tile caches if overwrite
    if overwrite:
        tiles_directory = settings.QGIS_SERVER_CONFIG['tiles_directory']
        basename, _ = os.path.splitext(qgis_layer.base_layer_path)
        basename = os.path.basename(basename)
        tiles_cache_path = os.path.join(tiles_directory, basename)
        try:
            shutil.rmtree(tiles_cache_path)
        except OSError:
            # The path doesn't exists yet
            pass


def qgis_server_pre_save_maplayer(instance, sender, **kwargs):
    logger.debug('QGIS Server Pre Save Map Layer %s' % instance.name)
    try:
        layer = Layer.objects.get(alternate=instance.name)
        if layer:
            instance.local = True
    except Layer.DoesNotExist:
        pass


def qgis_server_post_save_map(instance, sender, **kwargs):
    """Post Save Map Hook for QGIS Server

    This hook will creates QGIS Project for a given map.
    This hook also generates thumbnail link.
    """
    logger.debug('QGIS Server Post Save Map custom')
    map_id = instance.id
    map_layers = MapLayer.objects.filter(map__id=map_id)

    # Geonode map supports local layers and remote layers
    # Remote layers were provided from other OGC services, so we don't
    # deal with it at the moment.
    local_layers_name = [l.alternate for l in instance.local_layers]

    for ml in map_layers:
        if ml.name in local_layers_name:
            local = True
        else:
            local = False
        MapLayer.objects.filter(pk=ml.pk).update(local=local)

    layers = []
    for layer in instance.local_layers:
        try:
            if not layer.qgis_layer:
                raise QGISServerLayer.DoesNotExist
            layers.append(layer)
        except Layer.DoesNotExist:
            msg = 'No Layer found for typename: {0}'.format(layer.name)
            logger.debug(msg)
        except QGISServerLayer.DoesNotExist:
            msg = 'No QGIS Server Layer found for typename: {0}'.format(
                layer.name)
            logger.debug(msg)

    if not layers:
        # The signal is called too early, or the map has no layer yet.
        return

    base_url = settings.SITEURL
    # download map
    map_download_url = urljoin(
        base_url,
        reverse(
            'map_download',
            kwargs={'mapid': instance.id}))
    logger.debug('map_download_url: %s' % map_download_url)
    link_name = 'Download Data Layers'
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=link_name,
        defaults=dict(
            extension='html',
            mime='text/html',
            url=map_download_url,
            link_type='data'
        )
    )
    # WMC map layer workspace
    ogc_wmc_url = urljoin(
        base_url,
        reverse(
            'map_wmc',
            kwargs={'mapid': instance.id}))
    logger.debug('wmc_download_url: %s' % ogc_wmc_url)
    link_name = 'Download Web Map Context'
    link_mime = 'application/xml'
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=link_name,
        defaults=dict(
            extension='wmc.xml',
            mime=link_mime,
            url=ogc_wmc_url,
            link_type='data'
        )
    )

    # QLR map layer workspace
    ogc_qlr_url = urljoin(
        base_url,
        reverse(
            'map_download_qlr',
            kwargs={'mapid': instance.id}))
    logger.debug('qlr_map_download_url: %s' % ogc_qlr_url)
    link_name = 'QGIS layer file (.qlr)'
    link_mime = 'application/xml'
    Link.objects.update_or_create(
        resource=instance.resourcebase_ptr,
        name=link_name,
        defaults=dict(
            extension='qlr',
            mime=link_mime,
            url=ogc_qlr_url,
            link_type='data'
        )
    )

    # Set default bounding box based on all layers extents.
    # bbox format [xmin, xmax, ymin, ymax]
    bbox = instance.get_bbox_from_layers(layers)
    instance.set_bounds_from_bbox(bbox, instance.srid)
    Map.objects.filter(id=map_id).update(
        bbox_x0=instance.bbox_x0,
        bbox_x1=instance.bbox_x1,
        bbox_y0=instance.bbox_y0,
        bbox_y1=instance.bbox_y1,
        srid=instance.srid,
        zoom=instance.zoom,
        center_x=instance.center_x,
        center_y=instance.center_y)

    # Add chosen basemap to QGIS Project
    basemap_maplayer = MapLayer.objects.filter(
        map=instance,
        group='background',
        visibility=True).first()
    """:type: MapLayer"""
    tile_url = basemap_maplayer.ows_url

    # In case there is a subdomain URL, replace it
    tile_url = tile_url.replace('{s}.', '')

    # Check overwrite flag
    overwrite = getattr(instance, 'overwrite', False)

    # Create the QGIS Project
    qgis_map, created = QGISServerMap.objects.get_or_create(map=instance)

    # if it is newly created qgis_map, we should overwrite if existing files
    # exists.
    overwrite = created or overwrite
    response = create_qgis_project(
        layer=layers,
        qgis_project_path=qgis_map.qgis_project_path,
        overwrite=overwrite,
        basemap=tile_url,
        internal=True)

    logger.debug('Create project url: {url}'.format(url=response.url))
    logger.debug(
        'Creating the QGIS Project : %s -> %s' % (
            qgis_map.qgis_project_path, response.content))

    # add layer's individual style to qgis project
    for layer in layers:
        qgis_layer = layer.qgis_layer

        with qgis_layer.use_default_style_as_qml():
            # Insert styles to Map QGIS Project
            url_add_style = style_add_url(
                layer,
                style_name=layer.name,
                qgis_project_path=qgis_map.qgis_project_path)
            response = requests.get(url_add_style)
            if response.status_code != 200:
                logger.debug('Unable to add new style to qgs file')

            # Set default styles to Map QGIS Project
            url_set_default = style_set_default_url(
                layer,
                style_name=layer.name,
                qgis_project_path=qgis_map.qgis_project_path)
            response = requests.get(url_set_default)
            if response.status_code != 200:
                logger.debug('Unable to set default to the new style in qgs')

    # Generate map thumbnail
    create_qgis_server_thumbnail.delay(
        instance, overwrite=True)


def register_qgis_server_signals():
    """Helper function to register model signals."""
    if check_ogc_backend(qgis_server.BACKEND_PACKAGE):
        # Do it only if qgis_server is being used
        logger.debug('Register signals QGIS Server')
        signals.pre_save.connect(
            qgis_server_pre_save,
            dispatch_uid='Layer-qgis_server_pre_save',
            sender=Layer)
        signals.pre_delete.connect(
            qgis_server_pre_delete,
            dispatch_uid='Layer-qgis_server_pre_delete',
            sender=Layer)
        signals.post_save.connect(
            qgis_server_post_save,
            dispatch_uid='Layer-qgis_server_post_save',
            sender=Layer)
        signals.pre_save.connect(
            qgis_server_pre_save_maplayer,
            dispatch_uid='MapLayer-qgis_server_pre_save_maplayer',
            sender=MapLayer)
        signals.post_save.connect(
            qgis_server_post_save_map,
            dispatch_uid='Map-qgis_server_post_save_map',
            sender=Map)
        signals.post_delete.connect(
            qgis_server_layer_post_delete,
            dispatch_uid='QGISServerLayer-qgis_server_layer_post_delete',
            sender=QGISServerLayer)
