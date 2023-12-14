from django.contrib import admin

from .models import Release


@admin.register(Release)
class ReleaseAdmin(admin.ModelAdmin):
    list_display = [
        'project',
        'version',
        'date_released',
        'is_semver',
        'sort_epoch',
    ]

    list_filter = [
        'project',
    ]
