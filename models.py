import os
import logging
from django.db import models
from django.conf import settings

from geonode.layers.models import Layer
from geonode.maps.models import MapLayer

logger = logging.getLogger("geonode.qgis_server.models")

QGIS_LAYER_DIRECTORY = settings.QGIS_SERVER_CONFIG['layer_directory']

if not os.path.exists(QGIS_LAYER_DIRECTORY):
    os.mkdir(QGIS_LAYER_DIRECTORY)

class QGISServerLayer(models.Model):
    """Model for Layer in QGIS Server Backend.
    """

    accepted_format = [
        'tif', 'tiff', 'asc', 'shp', 'shx', 'dbf', 'prj', 'qml', 'xml']

    layer = models.OneToOneField(
        Layer,
        primary_key=True,
        name='layer'
    )
    base_layer_path = models.CharField(
        name='base_layer_path',
        verbose_name='Base Layer Path',
        help_text='Location of the base layer.',
        max_length=100
    )

    def delete_qgis_layer(self):
        """Delete all files related to this object from disk."""
        try:
            base_path = self.base_layer_path
            base_name, _ = os.path.splitext(base_path)
            for ext in QGISServerLayer.accepted_format:
                file_path = base_name + '.' + ext
                if os.path.exists(file_path):
                    os.remove(file_path)
        except QGISServerLayer.DoesNotExist:
            logger.debug('QGIS Server Layer not found. Not deleting.')
            pass

import geonode.qgis_server.signals