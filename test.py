"""Run with python -B -m unittest -v test. Fusion itself is stubbed."""
import base64
import copy
import sys
import tempfile
import types
import unittest
import zipfile
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from backup_catalog import BackupCatalog


class EventHandler:
    pass


adsk = types.ModuleType('adsk')
adsk.core = types.ModuleType('adsk.core')
adsk.drawing = types.ModuleType('adsk.drawing')
adsk.fusion = types.ModuleType('adsk.fusion')
adsk.autoTerminate = Mock()
adsk.terminate = Mock()
adsk.doEvents = Mock()
for name in ('CommandEventHandler', 'CommandCreatedEventHandler',
             'InputChangedEventHandler', 'CustomEventHandler'):
    setattr(adsk.core, name, EventHandler)
adsk.core.Application = Mock()
adsk.core.DataFile = object
adsk.core.Document = object
adsk.fusion.FusionDocument = Mock()
adsk.drawing.Drawing = Mock()
with patch.dict(sys.modules, {
    'adsk': adsk, 'adsk.core': adsk.core,
    'adsk.fusion': adsk.fusion, 'adsk.drawing': adsk.drawing,
}):
    import Exporter as E


class ExporterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.app = Mock()
        self.app.userInterface.activeCommand = 'SelectCommand'
        adsk.core.Application.get.return_value = self.app
        adsk.fusion.FusionDocument.cast.side_effect = lambda doc: doc
        adsk.drawing.Drawing.cast.side_effect = lambda product: product
        adsk.terminate.reset_mock()
        adsk.autoTerminate.reset_mock()
        adsk.doEvents.reset_mock(side_effect=True)
        self.enterContext(patch.object(E.time, 'sleep'))
        self.enterContext(patch.object(E, 'log_ui_resources', return_value=None))
        self.enterContext(patch.object(E, 'process_session_id', return_value='fusion-session-1'))
        self.enterContext(patch.object(E, 'log'))
        self.enterContext(patch.multiple(
            E, pending_export=None, options_closed=False, handlers=[],
            VERSION_SEPARATOR='_', log_fh=None, log_file=None,
        ))
        self.file = NS(name='part', versionNumber=4, fileExtension='f3d',
                       dateModified=1700000000, download=Mock(),
                       id='urn:lineage:part', versionId='urn:file:part?version=4')
        self.file.versions = [self.file]
        self.app.data.findFileById.return_value = self.file
        self.manager = Mock()
        for method, fmt in (
            ('createFusionArchiveExportOptions', 'f3d'),
            ('createSTEPExportOptions', 'step'),
            ('createPDFExportOptions', 'pdf'),
        ):
            getattr(self.manager, method).side_effect = (
                lambda path, fmt=fmt: NS(path=Path(path), format=fmt)
            )
        self.manager.execute.side_effect = self.write_export
        self.root = Mock()
        self.root.name = 'part'
        self.root.sketches = []
        self.root.occurrences = []
        self.root.createThumbnail.return_value.getAsBase64String.return_value = (
            base64.b64encode(b'preview').decode()
        )
        self.design = NS(exportManager=self.manager, rootComponent=self.root)
        self.document = Mock()
        self.document.design = self.design
        self.document.activate.return_value = True
        self.document.close.return_value = True
        self.app.documents.open.return_value = self.document
        self.app.activeProduct = self.design
        self.ctx = E.Ctx(
            app=self.app, folder=self.folder, formats=[E.Format.F3D, E.Format.STEP],
            projects_folders={}, use_active_folder=False, unhide_all=False,
            save_sketches=False, num_versions=0, export_non_design_files=True,
        )

    def write_export(self, options):
        if options.format == 'f3d':
            with zipfile.ZipFile(options.path, 'w') as archive:
                archive.writestr('model', b'design')
        else:
            options.path.write_bytes(b'exported geometry')
        return True

    def test_batch_exports_formats_with_one_open_and_close_then_skips(self):
        self.assertEqual(E.visit_file(self.ctx, self.file), E.Counter(saved=2))
        self.app.documents.open.assert_called_once_with(self.file, False)
        self.document.close.assert_called_once_with(False)
        self.assertTrue((self.folder / 'part_v4.step').is_file())
        with zipfile.ZipFile(self.folder / 'part_v4.f3d') as archive:
            self.assertEqual(archive.read('model'), b'design')
            self.assertEqual(archive.read('FusionAssetName[Active]/Previews/small.png'), b'preview')
        self.assertEqual(E.visit_file(self.ctx, self.file), E.Counter(skipped=2))
        self.app.documents.open.assert_called_once()

    def test_models_export_in_background_without_activating_document(self):
        # Export from the opened model even when another product is active.
        self.app.activeProduct = Mock()
        ctx = self.ctx._replace(formats=[E.Format.STEP])
        self.assertEqual(E.visit_file(ctx, self.file), E.Counter(saved=1))
        self.app.documents.open.assert_called_once_with(self.file, False)
        self.document.activate.assert_not_called()
        self.assertEqual((self.folder / 'part_v4.step').read_bytes(), b'exported geometry')

    def test_drawing_pdf_export_remains_visible_and_active(self):
        self.file.fileExtension = 'f2d'
        ctx = self.ctx._replace(formats=[E.Format.PDF])
        self.assertEqual(E.visit_file(ctx, self.file), E.Counter(saved=1))
        self.app.documents.open.assert_called_once_with(self.file, True)
        self.document.activate.assert_called_once()
        self.assertTrue((self.folder / 'part_v4.pdf').is_file())

    def test_fusion_cleanup_runs_between_documents(self):
        events = []
        def open_document(*args):
            events.append('open')
            return self.document
        def close_document(save):
            events.append('close')
            return True
        self.app.documents.open.side_effect = open_document
        self.document.close.side_effect = close_document
        adsk.doEvents.side_effect = lambda: events.append('pump')
        folder = NS(name='project', dataFiles=[self.file, self.file], dataFolders=[])
        E.visit_folder(self.ctx._replace(save_sketches=True, formats=[]), folder)
        self.assertEqual(events, ['open', 'close', 'pump', 'pump', 'open', 'close', 'pump', 'pump'])

    def test_failed_export_never_promotes_stale_or_partial_output(self):
        stale = self.folder / 'part_v4.tmp.step'
        stale.write_bytes(b'left by interrupted run')
        def fail(options):
            self.assertNotEqual(options.path, stale)
            options.path.write_bytes(b'partial')
            return False
        self.manager.execute.side_effect = fail
        ctx = self.ctx._replace(formats=[E.Format.STEP])
        self.assertEqual(E.visit_file(ctx, self.file), E.Counter(errored=1))
        self.assertEqual(list(self.folder.iterdir()), [stale])
        self.manager.execute.side_effect = self.write_export
        self.assertEqual(E.visit_file(ctx, self.file), E.Counter(saved=1))

    def test_missing_or_empty_output_is_error_even_when_api_returns_true(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                def export(options):
                    if empty:
                        options.path.touch()
                    return True
                self.manager.execute.side_effect = export
                self.assertEqual(E.visit_file(self.ctx, self.file), E.Counter(errored=2))
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_thumbnail_failure_does_not_publish_archive(self):
        self.root.createThumbnail.side_effect = RuntimeError('thumbnail failed')
        ctx = self.ctx._replace(formats=[E.Format.F3D])
        self.assertEqual(E.visit_file(ctx, self.file), E.Counter(errored=1))
        self.assertEqual(list(self.folder.iterdir()), [])
        self.document.close.assert_called_once_with(False)

    def test_thumbnail_is_added_before_final_archive_exists(self):
        def thumbnail(*args):
            self.assertFalse((self.folder / 'part_v4.f3d').exists())
            return NS(getAsBase64String=lambda: base64.b64encode(b'preview').decode())
        self.root.createThumbnail.side_effect = thumbnail
        self.assertEqual(E.visit_file(self.ctx._replace(formats=[E.Format.F3D]), self.file), E.Counter(saved=1))

    def test_interrupted_write_preserves_previous_export(self):
        dst = self.folder / 'part.step'
        dst.write_bytes(b'previous valid export')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with E.atomic_write(dst) as tmp:
                self.assertEqual(tmp.suffix, '.step')
                tmp.write_bytes(b'partial')
                raise RuntimeError('interrupted')
        self.assertEqual(dst.read_bytes(), b'previous valid export')
        self.assertEqual(list(self.folder.iterdir()), [dst])

    def test_drawing_failure_removes_partial_file(self):
        self.file.fileExtension = 'f2d'
        def fail(options):
            options.path.write_bytes(b'partial PDF')
            return False
        self.manager.execute.side_effect = fail
        self.assertEqual(E.visit_file(self.ctx._replace(formats=[E.Format.PDF]), self.file), E.Counter(errored=1))
        self.assertEqual(list(self.folder.iterdir()), [])
        self.document.close.assert_called_once_with(False)

    def test_sketch_failure_removes_partial_file(self):
        sketch = Mock()
        sketch.name = 'Sketch1'
        def fail(path):
            Path(path).write_bytes(b'partial DXF')
            return False
        sketch.saveAsDXF.side_effect = fail
        self.root.sketches = [sketch]
        doc = E.LazyDocument(self.ctx, self.file)
        self.assertEqual(E.visit_sketches(self.ctx, doc, self.root), E.Counter(errored=1))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_download_failure_removes_partial_file(self):
        self.file.fileExtension = 'txt'
        def fail(path, handler):
            Path(path).write_bytes(b'partial download')
            return False
        self.file.download.side_effect = fail
        self.assertEqual(E.visit_file(self.ctx, self.file), E.Counter(errored=1))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_empty_non_design_file_can_be_downloaded(self):
        self.file.fileExtension = 'txt'
        def download(path, handler):
            Path(path).touch()
            return True
        self.file.download.side_effect = download
        self.assertEqual(E.visit_file(self.ctx, self.file), E.Counter(saved=1))
        self.assertEqual((self.folder / 'part_v4.txt').stat().st_size, 0)

    def test_failed_open_does_not_use_invalid_document(self):
        self.app.documents.open.return_value = None
        ctx = self.ctx._replace(formats=[E.Format.STEP])
        self.assertEqual(E.visit_file(ctx, self.file), E.Counter(errored=1))
        self.manager.execute.assert_not_called()
        self.document.close.assert_not_called()

    def test_failed_activation_cannot_be_bypassed_by_next_format(self):
        self.file.fileExtension = 'f2d'
        self.document.activate.return_value = False
        doc = E.LazyDocument(self.ctx, self.file)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'could not activate'):
                doc.open()
        doc.close()
        self.manager.execute.assert_not_called()
        self.assertEqual(self.document.activate.call_count, 2)
        self.document.close.assert_called_once_with(False)

    def test_failed_close_stops_batch_before_next_file(self):
        folder = NS(name='project', dataFiles=[self.file, self.file], dataFolders=[])
        for failure in (False, RuntimeError('native close failed')):
            with self.subTest(failure=failure):
                self.app.documents.open.reset_mock()
                self.document.close.side_effect = failure if isinstance(failure, Exception) else None
                self.document.close.return_value = False
                with self.assertRaises(E.DocumentCloseError):
                    E.visit_folder(self.ctx._replace(save_sketches=True, formats=[]), folder)
                self.app.documents.open.assert_called_once()

    def test_document_close_is_idempotent(self):
        doc = E.LazyDocument(self.ctx, self.file)
        doc.open()
        doc.close()
        doc.close()
        self.document.close.assert_called_once_with(False)

    def test_root_subfolder_and_empty_selection_with_lists_and_sets(self):
        child = NS(id='child', name='child', dataFiles=[], dataFolders=[])
        root = NS(id='root', name='project', dataFiles=[], dataFolders=[child])
        self.app.data.dataProjects.itemById.return_value = NS(rootFolder=root)
        for factory in (list, set):
            for ids, expected in (
                ([], [(root, True)]), (['root'], [(root, False)]),
                (['child'], [(child, True)]),
                (['root', 'child'], [(root, False), (child, True)]),
            ):
                with self.subTest(factory=factory, ids=ids):
                    actual = [(folder, recurse) for _, folder, recurse in E.selected_folders(
                        self.ctx._replace(projects_folders={'project': factory(ids)}))]
                    self.assertEqual(actual, expected)

    def test_saved_context_can_be_loaded_repeatedly_without_mutation(self):
        original = self.ctx._replace(projects_folders={'project': []}).to_dict()
        before = copy.deepcopy(original)
        for _ in range(2):
            restored = E.Ctx.from_dict(original, self.app)
            self.assertEqual(restored.projects_folders, {'project': []})
            self.assertEqual(restored.formats, self.ctx.formats)
        self.assertEqual(original, before)

    def execute_options(self, space=False, projects=None):
        values = {
            'directory': str(self.folder), 'use_active_folder': False,
            'show_folders': False, 'unhide_all': False, 'save_sketches': False,
            'version_count': 0, 'all_versions': False,
            'version_separator_is_space': space, 'export_non_design_files': False,
            'subfolder_per_model': False,
            'max_documents': 450,
        }
        with patch.object(E, 'input_value', side_effect=lambda inputs, key: values[key]), \
             patch.object(E, 'input_selected', side_effect=lambda inputs, key: ['step'] if key == 'file_types' else []), \
             patch.object(E, 'make_projects_folders', return_value=projects or {}), \
             patch.object(E, 'save_last_settings'):
            E.ExporterCommandExecuteHandler().notify(NS(command=NS(commandInputs=object())))

    def options_command(self):
        self.app.userInterface.commandDefinitions.itemById.return_value = None
        command = self.app.userInterface.commandDefinitions.addButtonDefinition.return_value
        command.execute.return_value = True
        return command

    def test_batch_waits_until_command_destroy_callback_returns(self):
        self.options_command()
        captured = []
        with patch.object(E, 'run_main') as batch:
            def pump():
                self.execute_options()
                captured.append(E.pending_export)
                batch.assert_not_called()
                E.ExporterCommandDestroyHandler().notify(None)
                batch.assert_not_called()
                adsk.terminate.assert_not_called()
            adsk.doEvents.side_effect = pump
            E.run(None)
            batch.assert_called_once_with(captured[0])
        self.app.fireCustomEvent.assert_not_called()
        adsk.terminate.assert_called_once()
        self.assertIsNone(E.pending_export)

    def test_full_run_opens_and_closes_documents_after_callbacks_return(self):
        self.options_command()
        self.app.fireCustomEvent.return_value = False
        root = NS(id='root', name='project', dataFiles=[self.file], dataFolders=[])
        self.app.data.dataProjects.itemById.return_value = NS(rootFolder=root)
        in_callback = False
        ticks = 0

        def open_document(file, visible):
            self.assertFalse(in_callback)
            self.assertTrue(E.options_closed)
            self.assertFalse(visible)
            return self.document

        def close_document(save):
            self.assertFalse(in_callback)
            self.assertFalse(save)
            return True

        def pump():
            nonlocal in_callback, ticks
            if E.options_closed:
                return  # Fusion may also process deferred document cleanup.
            in_callback = True
            ticks += 1
            try:
                if ticks == 1:
                    self.execute_options(projects={'project': []})
                elif ticks == 3:
                    E.ExporterCommandDestroyHandler().notify(None)
                self.app.documents.open.assert_not_called()
            finally:
                in_callback = False

        self.app.documents.open.side_effect = open_document
        self.document.close.side_effect = close_document
        adsk.doEvents.side_effect = pump
        E.run(None)
        self.assertEqual(ticks, 3)
        self.app.documents.open.assert_called_once_with(self.file, False)
        self.document.close.assert_called_once_with(False)
        self.assertEqual((self.folder / 'project/part_v4.step').read_bytes(), b'exported geometry')
        summary = self.app.userInterface.messageBox.call_args.args[0]
        self.assertIn('Saved 1 files', summary)
        self.assertIn('Encountered 0 errors', summary)
        self.app.fireCustomEvent.assert_not_called()
        adsk.terminate.assert_called_once()

    def test_second_run_cancel_does_not_reuse_previous_settings(self):
        self.options_command()
        def accept():
            self.execute_options()
            E.ExporterCommandDestroyHandler().notify(None)
        adsk.doEvents.side_effect = accept
        with patch.object(E, 'run_main') as batch:
            E.run(None)
            batch.assert_called_once()
            adsk.doEvents.side_effect = lambda: E.ExporterCommandDestroyHandler().notify(None)
            E.run(None)
            batch.assert_called_once()
        self.assertEqual(adsk.terminate.call_count, 2)

    def test_cancel_does_not_queue_or_run_batch(self):
        self.options_command()
        adsk.doEvents.side_effect = lambda: E.ExporterCommandDestroyHandler().notify(None)
        with patch.object(E, 'run_main') as batch:
            E.run(None)
            batch.assert_not_called()
        self.app.fireCustomEvent.assert_not_called()
        adsk.terminate.assert_called_once()

    def test_custom_event_rejection_cannot_block_export(self):
        self.options_command()
        self.app.fireCustomEvent.return_value = False
        def pump():
            self.execute_options()
            E.ExporterCommandDestroyHandler().notify(None)
        adsk.doEvents.side_effect = pump
        with patch.object(E, 'run_main') as batch:
            E.run(None)
            batch.assert_called_once()
        self.app.registerCustomEvent.assert_not_called()
        self.app.fireCustomEvent.assert_not_called()
        self.app.userInterface.messageBox.assert_not_called()
        adsk.terminate.assert_called_once()

    def test_batch_exception_still_terminates_script(self):
        self.options_command()
        def pump():
            self.execute_options()
            E.ExporterCommandDestroyHandler().notify(None)
        adsk.doEvents.side_effect = pump
        with patch.object(E, 'run_main', side_effect=RuntimeError('batch failed')):
            E.run(None)
        self.app.userInterface.messageBox.assert_called_once()
        adsk.terminate.assert_called_once()

    def test_separator_can_be_switched_back_to_underscore(self):
        self.execute_options(space=True)
        self.assertEqual(E.VERSION_SEPARATOR, ' ')
        self.execute_options(space=False)
        self.assertEqual(E.VERSION_SEPARATOR, '_')

    def test_run_keeps_script_alive_before_dialog(self):
        command = self.options_command()
        def show_options():
            adsk.autoTerminate.assert_called_once_with(False)
            return True
        command.execute.side_effect = show_options
        adsk.doEvents.side_effect = lambda: E.ExporterCommandDestroyHandler().notify(None)
        E.run(None)
        command.execute.assert_called_once()
        self.app.userInterface.messageBox.assert_not_called()

    def test_failed_command_start_cleans_up_and_reports_error(self):
        command = self.options_command()
        command.execute.return_value = False
        E.run(None)
        self.app.userInterface.messageBox.assert_called_once()
        adsk.doEvents.assert_not_called()
        adsk.terminate.assert_called_once()

    def make_queue_file(self, name, version=1):
        file = NS(name=name, versionNumber=version, fileExtension='f3d',
                  dateModified=1700000000 + version, id=f'urn:lineage:{name}',
                  versionId=f'urn:file:{name}?version={version}')
        file.versions = [file]
        return file

    def queue_context(self, files, **settings):
        root = NS(id='root', name='project', dataFiles=files, dataFolders=[])
        self.app.data.dataProjects.itemById.return_value = NS(rootFolder=root)
        lookup = {version.versionId: version for file in files for version in file.versions}
        self.app.data.findFileById.side_effect = lambda version_id: lookup.get(version_id)
        ctx = self.ctx._replace(projects_folders={'project': []}, formats=[E.Format.STEP], max_documents=2)
        return ctx._replace(**settings)

    def run_queue(self, ctx, session='fusion-session-1'):
        with patch.object(E, 'init_logging'), patch.object(E, 'process_session_id', return_value=session):
            return E.main(ctx)

    def test_catalog_finishes_discovery_before_any_document_opens(self):
        files = [self.make_queue_file(name) for name in ('a', 'b', 'c')]
        ctx = self.queue_context(files)
        def open_document(*args):
            with closing(sqlite3.connect(self.folder / '.fusion-exporter.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0], 1)
            return self.document
        self.app.documents.open.side_effect = open_document
        result = self.run_queue(ctx)
        self.assertEqual((result.total, result.completed, result.remaining, result.opened), (3, 2, 1, 2))
        self.assertIn('limit', result.stop_reason)

    def test_next_invocation_resumes_without_reopening_completed_documents(self):
        files = [self.make_queue_file(name) for name in ('a', 'b', 'c')]
        ctx = self.queue_context(files, save_sketches=True)
        first = self.run_queue(ctx)
        self.assertEqual((first.completed, first.remaining), (2, 1))
        self.app.documents.open.reset_mock()
        same_session = self.run_queue(ctx)
        self.assertEqual((same_session.opened, same_session.session_opens, same_session.remaining), (0, 2, 1))
        self.app.documents.open.assert_not_called()
        restarted = self.run_queue(ctx, session='fusion-session-2')
        self.assertEqual((restarted.completed, restarted.remaining, restarted.opened), (3, 0, 1))
        self.app.documents.open.assert_called_once_with(files[2], False)

    def test_completed_sketches_are_skipped_without_opening_model(self):
        ctx = self.queue_context([self.file], save_sketches=True)
        sketch = Mock()
        sketch.name = 'Sketch1'
        def save(path):
            Path(path).write_bytes(b'DXF content')
            return True
        sketch.saveAsDXF.side_effect = save
        self.root.sketches = [sketch]
        self.assertEqual(self.run_queue(ctx).completed, 1)
        self.app.documents.open.reset_mock()
        self.app.data.findFileById.reset_mock()
        result = self.run_queue(ctx)
        self.assertEqual((result.opened, result.completed, result.remaining), (0, 1, 0))
        self.app.documents.open.assert_not_called()
        self.app.data.findFileById.assert_not_called()
        sketch.saveAsDXF.assert_called_once()

    def test_deleting_completed_output_puts_document_back_in_queue(self):
        ctx = self.queue_context([self.file])
        self.assertEqual(self.run_queue(ctx).completed, 1)
        (self.folder / 'project/part_v4.step').unlink()
        self.app.documents.open.reset_mock()
        result = self.run_queue(ctx)
        self.assertEqual((result.opened, result.completed), (1, 1))
        self.app.documents.open.assert_called_once()

    def test_existing_model_exports_are_adopted_without_opening_model(self):
        ctx = self.queue_context([self.file])
        destination = self.folder / 'project/part_v4.step'
        destination.parent.mkdir()
        destination.write_bytes(b'export from an earlier exporter')
        result = self.run_queue(ctx)
        self.assertEqual((result.total, result.completed, result.opened), (1, 1, 0))
        self.app.documents.open.assert_not_called()

    def test_partial_format_failure_stays_queued_and_retries_only_missing_format(self):
        ctx = self.queue_context([self.file], formats=[E.Format.F3D, E.Format.STEP])
        def export(options):
            if options.format == 'step':
                return False
            return self.write_export(options)
        self.manager.execute.side_effect = export
        first = self.run_queue(ctx)
        self.assertEqual((first.completed, first.failed, first.remaining), (0, 1, 1))
        self.manager.execute.reset_mock()
        self.manager.execute.side_effect = self.write_export
        result = self.run_queue(ctx)
        self.assertEqual((result.completed, result.remaining), (1, 0))
        self.manager.execute.assert_called_once()
        self.assertEqual(self.manager.execute.call_args.args[0].format, 'step')

    def test_native_crash_keeps_current_job_and_open_reservation(self):
        class NativeCrash(BaseException):
            pass
        ctx = self.queue_context([self.file], save_sketches=True)
        self.app.documents.open.side_effect = NativeCrash()
        with self.assertRaises(NativeCrash):
            self.run_queue(ctx)
        with BackupCatalog(self.folder) as catalog:
            job = catalog.db.execute('SELECT status FROM jobs').fetchone()
            self.assertEqual(job['status'], 'interrupted')
            self.assertEqual(catalog.session_opens('fusion-session-1'), 1)
        self.app.documents.open.side_effect = None
        result = self.run_queue(ctx, session='fusion-session-2')
        self.assertEqual((result.completed, result.remaining, result.opened), (1, 0, 1))

    def test_switching_to_all_versions_reuses_completed_latest_version(self):
        latest = self.make_queue_file('part', 2)
        older = self.make_queue_file('part', 1)
        latest.versions = [latest, older]
        ctx = self.queue_context([latest], save_sketches=True)
        self.assertEqual(self.run_queue(ctx).completed, 1)
        self.app.documents.open.reset_mock()
        result = self.run_queue(ctx._replace(num_versions=-1))
        self.assertEqual((result.total, result.completed, result.opened), (2, 2, 1))
        self.app.documents.open.assert_called_once_with(older, False)

    def test_new_cloud_version_is_added_to_catalog(self):
        first = self.make_queue_file('part', 1)
        ctx = self.queue_context([first])
        self.assertEqual(self.run_queue(ctx).completed, 1)
        updated = self.make_queue_file('part', 2)
        ctx = self.queue_context([updated])
        result = self.run_queue(ctx)
        self.assertEqual((result.total, result.completed, result.opened), (1, 1, 1))
        self.assertTrue((self.folder / 'project/part_v1.step').exists())
        self.assertTrue((self.folder / 'project/part_v2.step').exists())

    def test_queue_dialog_refresh_shows_counts_without_opening_documents(self):
        files = [self.make_queue_file(name) for name in ('a', 'b', 'c')]
        ctx = self.queue_context(files)
        status = NS(text='')
        inputs = Mock()
        inputs.itemById.return_value = status
        with patch.object(E, 'context_from_inputs', return_value=ctx), \
             patch.object(E, 'input_value', return_value=False):
            E.update_queue_display(inputs, refresh=True)
            self.assertIn('3 total | 0 completed | 3 remaining', status.text)
            self.app.documents.open.assert_not_called()
            self.run_queue(ctx)
            E.update_queue_display(inputs)
            self.assertIn('3 total | 2 completed | 1 remaining', status.text)
            self.assertIn('2/2 document opens', status.text)
            self.assertEqual(len(status.text.splitlines()), 2)

    def test_queue_dialog_does_not_show_stale_count_for_changed_options(self):
        ctx = self.queue_context([self.file])
        self.run_queue(ctx)
        inputs = Mock()
        status = NS(text='')
        inputs.itemById.return_value = status
        with patch.object(E, 'context_from_inputs', return_value=ctx._replace(save_sketches=True)), \
             patch.object(E, 'input_value', return_value=False):
            E.update_queue_display(inputs)
        self.assertIn('Options changed', status.text)

    def test_run_summary_distinguishes_pause_and_backup_complete(self):
        files = [self.make_queue_file(name) for name in ('a', 'b')]
        ctx = self.queue_context(files, max_documents=1)
        E.run_main(ctx)
        summary, title = self.app.userInterface.messageBox.call_args.args
        self.assertIn('Run finished', title)
        self.assertIn('Paused:', summary)
        self.assertIn('1 remaining', summary)
        self.assertNotIn('Backup complete.', summary)
        with patch.object(E, 'process_session_id', return_value='fusion-session-2'):
            E.run_main(ctx)
        summary = self.app.userInterface.messageBox.call_args.args[0]
        self.assertIn('Backup complete.', summary)
        self.assertIn('0 remaining', summary)

    def test_450_is_default_and_exact_open_allowance(self):
        self.assertEqual(self.ctx.max_documents, 450)
        with BackupCatalog(self.folder) as catalog:
            for _ in range(450):
                self.assertTrue(catalog.reserve_open('session', 450))
            self.assertFalse(catalog.reserve_open('session', 450))
            self.assertEqual(catalog.session_opens('session'), 450)
        with BackupCatalog(self.folder) as catalog:
            self.assertFalse(catalog.reserve_open('session', 450))
            self.assertTrue(catalog.reserve_open('restarted-session', 450))

    def test_deleted_sketch_is_reexported_even_when_model_export_exists(self):
        ctx = self.queue_context([self.file], save_sketches=True)
        sketch = Mock()
        sketch.name = 'Sketch1'
        def save(path):
            Path(path).write_bytes(b'DXF')
            return True
        sketch.saveAsDXF.side_effect = save
        self.root.sketches = [sketch]
        self.assertEqual(self.run_queue(ctx).completed, 1)
        (self.folder / 'project/part/Sketch1_v4.dxf').unlink()
        self.app.documents.open.reset_mock()
        result = self.run_queue(ctx)
        self.assertEqual((result.opened, result.completed), (1, 1))
        self.app.documents.open.assert_called_once()
        self.assertEqual(sketch.saveAsDXF.call_count, 2)

    def test_cataloging_error_never_reports_complete(self):
        ctx = self.queue_context([self.file])
        with patch.object(E, 'catalog_folder', return_value=iter([None])):
            E.run_main(ctx)
        summary = self.app.userInterface.messageBox.call_args.args[0]
        self.assertIn('Run finished.', summary)
        self.assertIn('Encountered 1 errors', summary)
        self.assertNotIn('Backup complete.', summary)
        self.app.documents.open.assert_not_called()
        inputs = Mock()
        status = NS(text='')
        inputs.itemById.return_value = status
        with patch.object(E, 'context_from_inputs', return_value=ctx), \
             patch.object(E, 'input_value', return_value=False):
            E.update_queue_display(inputs)
        self.assertIn('Queue incomplete: 1 cataloging errors', status.text)

    def test_interrupted_catalog_scan_rolls_back_previous_inventory(self):
        ctx = self.queue_context([self.file])
        with BackupCatalog(self.folder) as catalog:
            first_scan, _ = E.catalog_inventory(ctx, catalog, '_')
            payload = catalog.queue(first_scan)[0]['payload']
            with self.assertRaisesRegex(RuntimeError, 'discovery interrupted'):
                with catalog.scan():
                    catalog.discover(dict(payload, name='different'), 'broken-scan', 0)
                    raise RuntimeError('discovery interrupted')
            self.assertEqual(catalog.counts(first_scan)['total'], 1)
            self.assertEqual(catalog.counts('broken-scan')['total'], 0)
            self.assertEqual(catalog.preview(E.queue_options(ctx, '_'))['total'], 1)

    def test_high_ui_resource_use_pauses_before_open(self):
        ctx = self.queue_context([self.file])
        with patch.object(E, 'log_ui_resources', return_value=(7200, 100)):
            result = self.run_queue(ctx)
        self.assertEqual((result.completed, result.remaining, result.opened), (0, 1, 0))
        self.assertIn('resource use is high', result.stop_reason)
        self.app.documents.open.assert_not_called()

    def test_deselected_files_are_not_exported_from_old_queue(self):
        files = [self.make_queue_file(name) for name in ('a', 'b')]
        ctx = self.queue_context(files, max_documents=1)
        first = self.run_queue(ctx)
        self.assertEqual(first.remaining, 1)
        ctx = self.queue_context([files[0]], max_documents=1)
        self.app.documents.open.reset_mock()
        result = self.run_queue(ctx, session='fusion-session-2')
        self.assertEqual((result.total, result.remaining, result.opened), (1, 0, 0))
        self.app.documents.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
