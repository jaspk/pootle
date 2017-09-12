# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-08-17 09:16
from __future__ import unicode_literals

import json
import logging
import os
import posixpath
from functools import partial
from pathlib import PosixPath

import dirsync

from django.conf import settings
from django.db import migrations

from translate.lang.data import langcode_re


logger = logging.getLogger(__name__)


def _file_belongs_to_project(project, filename):
    ext = os.path.splitext(filename)[1][1:]
    filetype_extensions = list(
        project.filetypes.values_list(
            "extension__name", flat=True))
    template_extensions = list(
        project.filetypes.values_list(
            "template_extension__name", flat=True))
    return (
        ext in filetype_extensions
        or (ext in template_extensions))


def _detect_treestyle_and_path(Config, Language, project, proj_trans_path):
    dirlisting = os.walk(proj_trans_path)
    dirpath_, dirnames, filenames = dirlisting.next()

    if not dirnames:
        # No subdirectories
        if filter(partial(_file_belongs_to_project, project), filenames):
            # Translation files found, assume gnu
            return "gnu", ""

    # There are subdirectories
    languages = set(
        Language.objects.values_list("code", flat=True))
    lang_mapping_config = Config.objects.filter(
        content_type__model="project",
        object_pk=project.pk,
        key="pootle.core.lang_mappings").values_list(
            "value", flat=True).first()
    if lang_mapping_config:
        languages |= set(json.loads(lang_mapping_config).keys())
    has_subdirs = filter(
        (lambda dirname: (
            (dirname == 'templates'
             or langcode_re.match(dirname))
            and dirname in languages)),
        dirnames)
    if has_subdirs:
        return "nongnu", None

    # No language subdirs found, look for any translation file
    # in subdirs
    for dirpath_, dirnames, filenames in os.walk(proj_trans_path):
        if filter(partial(_file_belongs_to_project, project), filenames):
            return "gnu", dirpath_.replace(proj_trans_path, "")
    # Unsure
    return "nongnu", None


def _get_translation_mapping(Config, Language, project):
    old_translation_path = settings.POOTLE_TRANSLATION_DIRECTORY
    proj_trans_path = os.path.join(old_translation_path, project.code)
    old_treestyle, old_path = (
        _detect_treestyle_and_path(
            Config, Language, project, proj_trans_path)
        if project.treestyle in ["auto", "gnu"]
        else (project.treestyle, None))
    project.treestyle = "pootle_fs"
    if old_treestyle == "nongnu":
        return "/<language_code>/<dir_path>/<filename>.<ext>"
    else:
        return (
            "%s/<language_code>.<ext>"
            % (old_path and "/<dir_path>" or ""))


def _set_project_config(Language, Config, project_ct, project):
    old_translation_path = settings.POOTLE_TRANSLATION_DIRECTORY
    proj_trans_path = os.path.join(old_translation_path, project.code)
    configs = Config.objects.filter(
        content_type=project_ct,
        object_pk=project.pk)
    configs.delete()
    Config.objects.update_or_create(
        content_type=project_ct,
        object_pk=project.pk,
        key="pootle_fs.fs_url",
        defaults=dict(
            value=proj_trans_path))
    Config.objects.update_or_create(
        content_type=project_ct,
        object_pk=project.pk,
        key="pootle_fs.fs_type",
        defaults=dict(
            value="localfs"))
    if os.path.exists(proj_trans_path):
        Config.objects.update_or_create(
            content_type=project_ct,
            object_pk=project.pk,
            key="pootle_fs.translation_mappings",
            defaults=dict(
                value=dict(default=_get_translation_mapping(
                    Config, Language, project))))


def convert_to_localfs(apps, schema_editor):
    Project = apps.get_model("pootle_project.Project")
    Store = apps.get_model("pootle_store.Store")
    StoreFS = apps.get_model("pootle_fs.StoreFS")
    Config = apps.get_model("pootle_config.Config")
    Language = apps.get_model("pootle_language.Language")
    ContentType = apps.get_model("contenttypes.ContentType")
    project_ct = ContentType.objects.get_for_model(Project)
    old_translation_path = settings.POOTLE_TRANSLATION_DIRECTORY

    for project in Project.objects.exclude(treestyle="pootle_fs"):
        logger.debug("Converting project '%s' to pootle fs", project.code)
        proj_trans_path = str(PosixPath().joinpath(old_translation_path, project.code))
        proj_stores = Store.objects.filter(
            translation_project__project=project).exclude(file="").exclude(obsolete=True)
        old_treestyle, old_path = (
            _detect_treestyle_and_path(
                Config, Language, project, proj_trans_path)
            if project.treestyle in ["auto", "gnu"]
            else (project.treestyle, None))
        _set_project_config(Language, Config, project_ct, project)
        project.treestyle = "pootle_fs"
        project.save()

        if project.disabled:
            continue
        if not os.path.exists(proj_trans_path):
            logger.warn(
                "Missing project ('%s') translation directory '%s', "
                "skipped adding tracking",
                project.code,
                proj_trans_path)
            continue
        store_fs = StoreFS.objects.filter(
            store__translation_project__project=project)
        store_fs.delete()
        sfs = []
        templates = []
        for store in proj_stores:
            filepath = str(store.file)[len(project.code):]
            fullpath = str(
                PosixPath().joinpath(
                    proj_trans_path,
                    filepath.lstrip("/")))
            if not os.path.exists(fullpath):
                logger.warn(
                    "No file found at '%s', not adding tracking",
                    fullpath)
                continue
            if store.is_template and old_treestyle == "gnu":
                templates.append(store)
            sfs.append(
                StoreFS(
                    project=project,
                    store=store,
                    path=str(filepath),
                    pootle_path=store.pootle_path,
                    last_sync_hash=str(os.stat(fullpath).st_mtime),
                    last_sync_revision=store.last_sync_revision,
                    last_sync_mtime=store.file_mtime))
        if len(sfs):
            StoreFS.objects.bulk_create(sfs, batch_size=1000)
        if old_treestyle == "gnu" and len(templates) == 1:
            template = templates[0]
            template_name, __ = posixpath.splitext(template.name)
            if template_name != "templates":
                try:
                    mapping = Config.objects.get(
                        content_type=project_ct,
                        object_pk=project.pk,
                        key="pootle.core.language_mapping")
                except Config.DoesNotExist:
                    mapping = {}
                mapping[template_name] = "templates"
                Config.objects.update_or_create(
                    content_type=project_ct,
                    object_pk=project.pk,
                    key="pootle.core.language_mapping",
                    defaults=dict(value=mapping))
        logger.debug(
            "Tracking added for %s/%s stores in project '%s'",
            len(sfs),
            proj_stores.count(),
            project.code)
        fs_temp = os.path.join(
            settings.POOTLE_FS_WORKING_PATH, project.code)
        dirsync.sync(
            str(proj_trans_path),
            fs_temp,
            "sync",
            create=True,
            purge=True,
            logger=logging.getLogger(dirsync.__name__))


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('pootle_fs', '0001_initial'),
        ('pootle_format', '0003_remove_extra_indeces'),
        ('pootle_config', '0001_initial'),
        ('pootle_store', '0013_set_store_filetype_again'),
        ('pootle_project', '0016_change_treestyle_choices_label'),
    ]

    operations = [
        migrations.RunPython(convert_to_localfs),
    ]