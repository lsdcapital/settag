from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast

from settag.analyzer import EssentiaGenreAnalyzer, EssentiaTaskAnalyzer
from settag.tasks import AnalysisTask, ordered_tasks
from settag.workflow import (
    AnalysisBatch,
    CancelCallback,
    ProgressCallback,
    analyze_paths,
)

AnalyzerFactory = Callable[[Path, tuple[AnalysisTask, ...]], Any]


class ProcessContext(Protocol):
    def Pipe(self, duplex: bool = True) -> tuple[Connection, Connection]: ...

    def Process(
        self,
        *,
        target: Callable[..., object],
        args: tuple[object, ...],
        name: str,
        daemon: bool,
    ) -> BaseProcess: ...


class AnalysisWorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class _AnalyzeRequest:
    path: Path
    top: int
    threshold: float


@dataclass(frozen=True)
class _AnalysisResult:
    batch: AnalysisBatch


@dataclass(frozen=True)
class _AnalysisError:
    error_type: str
    message: str


@dataclass(frozen=True)
class _Shutdown:
    pass


def _create_analyzer(
    model_dir: Path,
    tasks: tuple[AnalysisTask, ...],
) -> EssentiaGenreAnalyzer | EssentiaTaskAnalyzer:
    if tasks == ("genre",):
        return EssentiaGenreAnalyzer(model_dir)
    return EssentiaTaskAnalyzer(model_dir, tasks)


def _analysis_worker_main(
    connection: Connection,
    model_dir: Path,
    tasks: tuple[AnalysisTask, ...],
    analyzer_factory: AnalyzerFactory,
) -> None:
    analyzer: Any | None = None
    try:
        while True:
            try:
                request = connection.recv()
            except EOFError:
                return

            if isinstance(request, _Shutdown):
                return
            if not isinstance(request, _AnalyzeRequest):
                response: _AnalysisResult | _AnalysisError = _AnalysisError(
                    "RuntimeError",
                    f"Analyzer worker received an invalid request: {type(request).__name__}",
                )
            else:
                try:
                    if analyzer is None:
                        analyzer = analyzer_factory(model_dir, tasks)
                    batch = analyze_paths(
                        (request.path,),
                        analyzer=analyzer,
                        top=request.top,
                        threshold=request.threshold,
                    )
                    response = _AnalysisResult(batch)
                except Exception as error:
                    response = _AnalysisError(type(error).__name__, str(error))

            try:
                connection.send(response)
            except (BrokenPipeError, EOFError, OSError):
                return
    finally:
        connection.close()


class SubprocessAnalysisLoader:
    """Run serial analysis in one persistent spawned process.

    Textual still calls this loader from its thread worker. That thread blocks
    only on IPC while native Essentia/TensorFlow work runs in another process,
    keeping the UI event loop independent from the analyzer's GIL and CPU use.
    """

    def __init__(
        self,
        model_dir: Path,
        tasks: Sequence[AnalysisTask],
        *,
        top: int,
        threshold: float,
        analyzer_factory: AnalyzerFactory = _create_analyzer,
        context: ProcessContext | None = None,
        poll_interval: float = 0.05,
        shutdown_timeout: float = 5.0,
    ) -> None:
        selected = ordered_tasks(tasks)
        if not selected:
            raise ValueError("analysis worker requires at least one task")
        if poll_interval <= 0:
            raise ValueError("analysis worker poll interval must be positive")
        if shutdown_timeout < 0:
            raise ValueError("analysis worker shutdown timeout cannot be negative")

        self.model_dir = model_dir.expanduser().resolve()
        self.tasks = selected
        self.top = top
        self.threshold = threshold
        self._analyzer_factory = analyzer_factory
        self._context = context or cast(ProcessContext, get_context("spawn"))
        self._poll_interval = poll_interval
        self._shutdown_timeout = shutdown_timeout
        self._connection: Connection | None = None
        self._process: BaseProcess | None = None
        self._lock = Lock()
        self._closed = False

    def __call__(
        self,
        paths: Sequence[Path],
        on_progress: ProgressCallback,
        should_cancel: CancelCallback,
    ) -> AnalysisBatch:
        planned = []
        failures = []
        selected_paths = tuple(paths)
        cancelled = False

        with self._lock:
            if self._closed:
                raise AnalysisWorkerError("Analyzer worker is closed")
            self._ensure_started()

            for index, path in enumerate(selected_paths, start=1):
                if should_cancel():
                    cancelled = True
                    break

                response = self._analyze(path)
                if isinstance(response, _AnalysisError):
                    raise AnalysisWorkerError(f"{response.error_type}: {response.message}")

                planned.extend(response.batch.planned)
                failures.extend(response.batch.failures)
                on_progress(index, len(selected_paths), path)
                if should_cancel():
                    cancelled = True
                    break

        return AnalysisBatch(
            planned=tuple(planned),
            failures=tuple(failures),
            cancelled=cancelled,
        )

    def start(self) -> None:
        """Start the lightweight worker before a terminal UI owns the process."""
        with self._lock:
            if self._closed:
                raise AnalysisWorkerError("Analyzer worker is closed")
            self._ensure_started()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
            process = self._process
            self._connection = None
            self._process = None

            if connection is not None:
                if process is not None and process.is_alive():
                    with suppress(BrokenPipeError, EOFError, OSError):
                        connection.send(_Shutdown())
                connection.close()

            if process is None:
                return
            process.join(self._shutdown_timeout)
            if process.is_alive():
                process.terminate()
                process.join()
            process.close()

    def __enter__(self) -> SubprocessAnalysisLoader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ensure_started(self) -> None:
        if self._process is not None:
            if self._process.is_alive():
                return
            exit_code = self._process.exitcode
            self._discard_worker()
            raise AnalysisWorkerError(
                f"Analyzer worker stopped unexpectedly (exit code {exit_code})"
            )

        parent_connection, child_connection = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_analysis_worker_main,
            args=(
                child_connection,
                self.model_dir,
                self.tasks,
                self._analyzer_factory,
            ),
            name="settag-analyzer",
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            parent_connection.close()
            child_connection.close()
            raise
        child_connection.close()
        self._connection = parent_connection
        self._process = process

    def _analyze(self, path: Path) -> _AnalysisResult | _AnalysisError:
        connection = self._connection
        process = self._process
        assert connection is not None
        assert process is not None

        try:
            connection.send(
                _AnalyzeRequest(
                    path=path.expanduser().resolve(),
                    top=self.top,
                    threshold=self.threshold,
                )
            )
        except (BrokenPipeError, EOFError, OSError) as error:
            self._raise_worker_failure(process, error)

        while True:
            try:
                if connection.poll(self._poll_interval):
                    response = connection.recv()
                    break
            except (BrokenPipeError, EOFError, OSError) as error:
                self._raise_worker_failure(process, error)
            if not process.is_alive():
                self._raise_worker_failure(process)

        if not isinstance(response, (_AnalysisResult, _AnalysisError)):
            raise AnalysisWorkerError(
                f"Analyzer worker returned an invalid response: {type(response).__name__}"
            )
        return response

    def _raise_worker_failure(
        self,
        process: BaseProcess,
        error: BaseException | None = None,
    ) -> None:
        exit_code = process.exitcode
        self._discard_worker(terminate=process.is_alive())
        detail = f": {error}" if error is not None else ""
        raise AnalysisWorkerError(
            f"Analyzer worker stopped unexpectedly (exit code {exit_code}){detail}"
        )

    def _discard_worker(self, *, terminate: bool = False) -> None:
        connection = self._connection
        process = self._process
        self._connection = None
        self._process = None
        if connection is not None:
            connection.close()
        if process is None:
            return
        if terminate and process.is_alive():
            process.terminate()
        process.join()
        process.close()
