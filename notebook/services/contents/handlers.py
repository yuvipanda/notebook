"""Tornado handlers for the contents web service.

Preliminary documentation at https://github.com/ipython/ipython/wiki/IPEP-27%3A-Contents-Service
"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import json

from tornado import gen, web

from notebook.utils import maybe_future, url_path_join, url_escape, eventlogging_schema_fqn
from jupyter_client.jsonutil import date_default

from notebook.base.handlers import (
    IPythonHandler, APIHandler, path_regex,
)


def validate_model(model, expect_content):
    """
    Validate a model returned by a ContentsManager method.

    If expect_content is True, then we expect non-null entries for 'content'
    and 'format'.
    """
    required_keys = {
        "name",
        "path",
        "type",
        "writable",
        "created",
        "last_modified",
        "mimetype",
        "content",
        "format",
    }
    missing = required_keys - set(model.keys())
    if missing:
        raise web.HTTPError(
            500,
            u"Missing Model Keys: {missing}".format(missing=missing),
        )

    maybe_none_keys = ['content', 'format']
    if expect_content:
        errors = [key for key in maybe_none_keys if model[key] is None]
        if errors:
            raise web.HTTPError(
                500,
                u"Keys unexpectedly None: {keys}".format(keys=errors),
            )
    else:
        errors = {
            key: model[key]
            for key in maybe_none_keys
            if model[key] is not None
        }
        if errors:
            raise web.HTTPError(
                500,
                u"Keys unexpectedly not None: {keys}".format(keys=errors),
            )


class ContentsHandler(APIHandler):

    def location_url(self, path):
        """Return the full URL location of a file.

        Parameters
        ----------
        path : unicode
            The API path of the file, such as "foo/bar.txt".
        """
        return url_path_join(
            self.base_url, 'api', 'contents', url_escape(path)
        )

    def _finish_model(self, model, location=True):
        """Finish a JSON request with a model, setting relevant headers, etc."""
        if location:
            location = self.location_url(model['path'])
            self.set_header('Location', location)
        self.set_header('Last-Modified', model['last_modified'])
        self.set_header('Content-Type', 'application/json')
        self.finish(json.dumps(model, default=date_default))

    @web.authenticated
    @gen.coroutine
    def get(self, path=''):
        """Return a model for a file or directory.

        A directory model contains a list of models (without content)
        of the files and directories it contains.
        """
        path = path or ''
        type = self.get_query_argument('type', default=None)
        if type not in {None, 'directory', 'file', 'notebook'}:
            raise web.HTTPError(400, u'Type %r is invalid' % type)

        format = self.get_query_argument('format', default=None)
        if format not in {None, 'text', 'base64'}:
            raise web.HTTPError(400, u'Format %r is invalid' % format)
        content = self.get_query_argument('content', default='1')
        if content not in {'0', '1'}:
            raise web.HTTPError(400, u'Content %r is invalid' % content)
        content = int(content)
        
        model = yield maybe_future(self.contents_manager.get(
            path=path, type=type, format=format, content=content,
        ))
        validate_model(model, expect_content=content)
        self._finish_model(model, location=False)

        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            self.get_event_data({ 'action': 'get', 'path': model['path'] })
        )

    @web.authenticated
    @gen.coroutine
    def patch(self, path=''):
        """PATCH renames a file or directory without re-uploading content."""
        cm = self.contents_manager
        model = self.get_json_body()
        if model is None:
            raise web.HTTPError(400, u'JSON body missing')
        self.log.info(model)
        model = yield maybe_future(cm.update(model, path))
        validate_model(model, expect_content=False)
        self._finish_model(model)
        self.log.info(model)

        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            # FIXME: 'path' always has a leading slash, while model['path'] does not.
            # What to do here for source_path? path munge manually? Eww
            self.get_event_data(
                { 'action': 'rename', 'path': model['path'], 'source_path': path }
            )
        )
    
    @gen.coroutine
    def _copy(self, copy_from, copy_to=None):
        """Copy a file, optionally specifying a target directory."""
        self.log.info(u"Copying {copy_from} to {copy_to}".format(
            copy_from=copy_from,
            copy_to=copy_to or '',
        ))
        model = yield maybe_future(self.contents_manager.copy(copy_from, copy_to))
        self.set_status(201)
        validate_model(model, expect_content=False)
        self._finish_model(model)
        
        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            self.get_event_data(
                { 'action': 'copy', 'path': model['path'], 'source_path': copy_from }
            )
        )

    @gen.coroutine
    def _upload(self, model, path):
        """Handle upload of a new file to path"""
        self.log.info(u"Uploading file to %s", path)
        model = yield maybe_future(self.contents_manager.new(model, path))
        self.set_status(201)
        validate_model(model, expect_content=False)
        self._finish_model(model)

        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            self.get_event_data({ 'action': 'upload', 'path': model['path'] })
        )
    
    @gen.coroutine
    def _new_untitled(self, path, type='', ext=''):
        """Create a new, empty untitled entity"""
        self.log.info(u"Creating new %s in %s", type or 'file', path)
        model = yield maybe_future(self.contents_manager.new_untitled(path=path, type=type, ext=ext))
        self.set_status(201)
        validate_model(model, expect_content=False)
        self._finish_model(model)

        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            # Set path to path of created object, not directory it was created in
            self.get_event_data({ 'action': 'create', 'path': model['path'] })
        )
    
    @gen.coroutine
    def _save(self, model, path):
        """Save an existing file."""
        chunk = model.get("chunk", None) 
        if not chunk or chunk == -1:  # Avoid tedious log information
            self.log.info(u"Saving file at %s", path)  
        model = yield maybe_future(self.contents_manager.save(model, path))
        validate_model(model, expect_content=False)
        self._finish_model(model)

        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            self.get_event_data({ 'action': 'save', 'path': model['path'] })
        )

    @web.authenticated
    @gen.coroutine
    def post(self, path=''):
        """Create a new file in the specified path.

        POST creates new files. The server always decides on the name.

        POST /api/contents/path
          New untitled, empty file or directory.
        POST /api/contents/path
          with body {"copy_from" : "/path/to/OtherNotebook.ipynb"}
          New copy of OtherNotebook in path
        """

        cm = self.contents_manager

        file_exists = yield maybe_future(cm.file_exists(path))
        if file_exists:
            raise web.HTTPError(400, "Cannot POST to files, use PUT instead.")

        dir_exists = yield maybe_future(cm.dir_exists(path))
        if not dir_exists:
            raise web.HTTPError(404, "No such directory: %s" % path)

        model = self.get_json_body()

        if model is not None:
            copy_from = model.get('copy_from')
            ext = model.get('ext', '')
            type = model.get('type', '')
            if copy_from:
                yield self._copy(copy_from, path)
            else:
                yield self._new_untitled(path, type=type, ext=ext)
        else:
            yield self._new_untitled(path)

    @web.authenticated
    @gen.coroutine
    def put(self, path=''):
        """Saves the file in the location specified by name and path.

        PUT is very similar to POST, but the requester specifies the name,
        whereas with POST, the server picks the name.

        PUT /api/contents/path/Name.ipynb
          Save notebook at ``path/Name.ipynb``. Notebook structure is specified
          in `content` key of JSON request body. If content is not specified,
          create a new empty notebook.
        """
        model = self.get_json_body()
        if model:
            if model.get('copy_from'):
                raise web.HTTPError(400, "Cannot copy with PUT, only POST")
            exists = yield maybe_future(self.contents_manager.file_exists(path))
            if exists:
                yield maybe_future(self._save(model, path))
            else:
                yield maybe_future(self._upload(model, path))
        else:
            yield maybe_future(self._new_untitled(path))

    @web.authenticated
    @gen.coroutine
    def delete(self, path=''):
        """delete a file in the given path"""
        cm = self.contents_manager
        self.log.warning('delete %s', path)
        yield maybe_future(cm.delete(path))
        self.set_status(204)
        self.finish()

        # Emit event.
        self.eventlog.record_event(
            eventlogging_schema_fqn('contentsmanager-actions'), 1,
            self.get_event_data({ 'action': 'delete', 'path': path })
        )


class CheckpointsHandler(APIHandler):

    @web.authenticated
    @gen.coroutine
    def get(self, path=''):
        """get lists checkpoints for a file"""
        cm = self.contents_manager
        checkpoints = yield maybe_future(cm.list_checkpoints(path))
        data = json.dumps(checkpoints, default=date_default)
        self.finish(data)

    @web.authenticated
    @gen.coroutine
    def post(self, path=''):
        """post creates a new checkpoint"""
        cm = self.contents_manager
        checkpoint = yield maybe_future(cm.create_checkpoint(path))
        data = json.dumps(checkpoint, default=date_default)
        location = url_path_join(self.base_url, 'api/contents',
            url_escape(path), 'checkpoints', url_escape(checkpoint['id']))
        self.set_header('Location', location)
        self.set_status(201)
        self.finish(data)


class ModifyCheckpointsHandler(APIHandler):

    @web.authenticated
    @gen.coroutine
    def post(self, path, checkpoint_id):
        """post restores a file from a checkpoint"""
        cm = self.contents_manager
        yield maybe_future(cm.restore_checkpoint(checkpoint_id, path))
        self.set_status(204)
        self.finish()

    @web.authenticated
    @gen.coroutine
    def delete(self, path, checkpoint_id):
        """delete clears a checkpoint for a given file"""
        cm = self.contents_manager
        yield maybe_future(cm.delete_checkpoint(checkpoint_id, path))
        self.set_status(204)
        self.finish()


class NotebooksRedirectHandler(IPythonHandler):
    """Redirect /api/notebooks to /api/contents"""
    SUPPORTED_METHODS = ('GET', 'PUT', 'PATCH', 'POST', 'DELETE')

    def get(self, path):
        self.log.warning("/api/notebooks is deprecated, use /api/contents")
        self.redirect(url_path_join(
            self.base_url,
            'api/contents',
            path
        ))

    put = patch = post = delete = get


class TrustNotebooksHandler(IPythonHandler):
    """ Handles trust/signing of notebooks """

    @web.authenticated
    @gen.coroutine
    def post(self,path=''):
        cm = self.contents_manager
        yield maybe_future(cm.trust_notebook(path))
        self.set_status(201)
        self.finish()
#-----------------------------------------------------------------------------
# URL to handler mappings
#-----------------------------------------------------------------------------


_checkpoint_id_regex = r"(?P<checkpoint_id>[\w-]+)"

default_handlers = [
    (r"/api/contents%s/checkpoints" % path_regex, CheckpointsHandler),
    (r"/api/contents%s/checkpoints/%s" % (path_regex, _checkpoint_id_regex),
        ModifyCheckpointsHandler),
    (r"/api/contents%s/trust" % path_regex, TrustNotebooksHandler),
    (r"/api/contents%s" % path_regex, ContentsHandler),
    (r"/api/notebooks/?(.*)", NotebooksRedirectHandler),
]
