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
from django.contrib import admin

from geonode.layers.models import Layer
from geonode.layers import admin as layer_admin


class LayerAdminForm(layer_admin.LayerAdminForm):

    class Meta:
        model = Layer
        fields = '__all__'
        exclude = ['store', 'styles']


class LayerAdmin(layer_admin.LayerAdmin):

    form = LayerAdminForm


admin.site.unregister(Layer)
admin.site.register(Layer, LayerAdmin)
