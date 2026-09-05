"""The independent metadata-hygiene review app.

THESIS: hygiene is a field-level inspection bench, never an automatic broom.
OWN-WORLD: Booth Compass surfaces, one Ember cursor, dense metadata rows.
STORY: see the suspicious value, understand the rule, choose, verify, clean.
FIRST VIEWPORT: tracks group their proposed fixes; exact evidence stays one keystroke away.
FORM: an Operate-mode extension of SetTag's incumbent track-review workspace.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from settag.hygiene import (
    HygieneBatch,
    HygieneFailure,
    HygieneFinding,
    HygienePlan,
    HygieneSummary,
    HygieneTrack,
    PartialHygieneWriteError,
    PreparedHygiene,
    apply_hygiene,
    inspect_hygiene_paths,
    plan_hygiene_track,
    preflight_hygiene,
    summarize_hygiene,
)
from settag.journal import BatchRecorder, WriteJournal
from settag.tui.entries import TuiOutcome
from settag.tui.screens import ConfirmWriteScreen, ErrorScreen
from settag.tui.style import APP_CSS


@dataclass(frozen=True)
class HygieneReviewRow:
    path: Path
    track: HygieneTrack | None = None
    finding: HygieneFinding | None = None
    failure: HygieneFailure | None = None

    @property
    def is_selectable(self) -> bool:
        return self.track is not None and self.finding is not None


class HygieneTree(Tree[int | Path]):
    """Track branches with independently selectable cleanup suggestions."""

    BINDINGS = [
        Binding("space", "app.toggle_finding", "Toggle"),
        Binding("enter", "select_cursor", "Expand/Details"),
        Binding("left", "collapse_or_parent", "Collapse", show=False),
        Binding("right", "expand_or_child", "Expand", show=False),
    ]

    def action_collapse_or_parent(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.children and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.move_cursor(node.parent)

    def action_expand_or_child(self) -> None:
        node = self.cursor_node
        if node is None or not node.children:
            return
        if not node.is_expanded:
            node.expand()
        else:
            self.move_cursor(node.children[0])


class HygieneApp(App[TuiOutcome]):
    """Review suspicious text fields without loading an analysis model."""

    TITLE = "SetTag Hygiene"
    ENABLE_COMMAND_PALETTE = False
    CSS = APP_CSS
    BINDINGS = [
        Binding("space", "toggle_finding", "Toggle"),
        Binding("a", "toggle_all", "All/None"),
        Binding("i", "toggle_details", "Details"),
        Binding("w", "write", "Clean"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        source: Path,
        paths: Sequence[Path],
        batch: HygieneBatch | None = None,
        journal: WriteJournal,
    ) -> None:
        super().__init__()
        self.source = source.expanduser().resolve()
        self.paths = tuple(paths)
        self.batch = batch
        self.journal = journal
        self.rows: list[HygieneReviewRow] = []
        self.selected: set[int] = set()
        self._finding_nodes: dict[int, TreeNode[int | Path]] = {}
        self._track_nodes: dict[Path, TreeNode[int | Path]] = {}
        self.busy = batch is None
        self._pending: tuple[HygienePlan, ...] = ()
        self._pending_prepared: tuple[PreparedHygiene, ...] = ()
        if batch is not None:
            self._rebuild_rows(select_all=True)
        self.sub_title = "Scanning metadata" if batch is None else "Review suspicious metadata"

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="hygiene-main"):
            yield Static("", markup=False, id="context")
            with Horizontal(id="workspace"):
                with Vertical(id="tracks-pane"):
                    yield Static(
                        "Tracks and proposed fixes",
                        classes="section-title",
                    )
                    yield HygieneTree("Tracks", id="hygiene-tree")
                with Vertical(id="inspector-pane"):
                    yield Static("Finding details", classes="section-title")
                    with VerticalScroll(id="inspector-scroll", can_focus=True):
                        yield Static("", markup=False, id="inspector")
            yield Static("", markup=False, id="status")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one(HygieneTree)
        tree.show_root = False
        tree.auto_expand = False
        self._update_layout(self.size.width)
        self._rebuild_tree()
        if self.batch is None:
            self._load_hygiene()

    def on_resize(self, event: events.Resize) -> None:
        self._update_layout(event.size.width)

    def _update_layout(self, width: int) -> None:
        self.set_class(width < 90, "narrow")

    def _rebuild_rows(self, *, select_all: bool) -> None:
        assert self.batch is not None
        finding_rows = [
            HygieneReviewRow(path=track.path, track=track, finding=finding)
            for track in self.batch.tracks
            for finding in track.findings
        ]
        failure_rows = [
            HygieneReviewRow(path=failure.path, failure=failure) for failure in self.batch.failures
        ]
        self.rows = [*finding_rows, *failure_rows]
        self.selected = (
            {index for index, row in enumerate(self.rows) if row.is_selectable}
            if select_all
            else set()
        )

    def _finding_label(self, index: int) -> Text:
        finding = self.rows[index].finding
        assert finding is not None
        marker = "[x]" if index in self.selected else "[ ]"
        operation = "Update values" if finding.after else "Remove tag"
        return Text(f"{marker} {finding.label} → {operation} · {finding.reason_text}")

    def _track_label(self, path: Path) -> Text:
        node = self._track_nodes[path]
        indices = {child.data for child in node.children if isinstance(child.data, int)}
        checked = len(indices & self.selected)
        marker = "[x]" if checked == len(indices) else "[-]" if checked else "[ ]"
        return Text(f"{marker} {path.name} · {checked}/{len(indices)} checked", style="bold")

    def _rebuild_tree(self, *, message: str | None = None) -> None:
        tree = self.query_one(HygieneTree)
        tree.clear()
        self._finding_nodes.clear()
        self._track_nodes.clear()
        if self.batch is None:
            tree.root.add_leaf("Scanning metadata…")
            self._update_context()
            self._update_status(message or "Preparing metadata scan…")
            self.query_one("#inspector", Static).update(
                "Scanning comment-like and generated text fields…"
            )
            return
        for index, row in enumerate(self.rows):
            if row.finding is not None:
                if row.path not in self._track_nodes:
                    self._track_nodes[row.path] = tree.root.add(
                        Text(row.path.name), data=row.path, expand=True
                    )
                parent = self._track_nodes[row.path]
                self._finding_nodes[index] = parent.add_leaf(self._finding_label(index), data=index)
            else:
                assert row.failure is not None
                tree.root.add_leaf(Text(f"! {row.path.name} · Inspection error"), data=index)
        for path, node in self._track_nodes.items():
            node.set_label(self._track_label(path))
        if not self.rows:
            tree.root.add_leaf("No cleanup needed. No suspicious metadata found.")
            self.query_one("#inspector", Static).update(
                "No cleanup needed. No suspicious metadata found in the scanned files."
            )
        tree.root.expand()
        self._update_context()
        self._update_status(message)
        tree.focus()
        if tree.root.children[0].data is not None:
            self._update_inspector(tree.root.children[0].data)
        self.call_after_refresh(tree.move_cursor, tree.root.children[0])

    def _update_context(self) -> None:
        if self.batch is None:
            self.query_one("#context", Static).update(
                f"{self.source}\nScanning metadata without loading an analysis model"
            )
            return
        self.query_one("#context", Static).update(
            f"{self.batch.affected_track_count} affected of {self.batch.track_count} scanned"
            f"  ·  {self.batch.finding_count} suggestions"
            f"  ·  {self.batch.failure_count} errors\n{self.source}"
        )

    def _update_status(self, message: str | None = None) -> None:
        if self.busy:
            if message is not None:
                self.query_one("#status", Static).update(message)
            return
        selected_tracks = {self.rows[index].path for index in self.selected}
        base = (
            f"{len(self.selected)} fixes checked across {len(selected_tracks)} tracks"
            "  ·  Space toggles a fix or track"
        )
        self.query_one("#status", Static).update(f"{message}  ·  {base}" if message else base)

    def _current_item(self) -> int | Path | None:
        node = self.query_one(HygieneTree).cursor_node
        return node.data if node is not None else None

    @work(thread=True, exclusive=True, group="hygiene-scan", exit_on_error=False)
    def _load_hygiene(self) -> None:
        batch = inspect_hygiene_paths(self.paths, on_progress=self._scan_progress_from_worker)
        self.call_from_thread(self._scan_complete, batch)

    def _scan_progress_from_worker(self, completed: int, total: int, path: Path) -> None:
        self.call_from_thread(
            self._update_status,
            f"Inspecting {completed} of {total}: {path.name}",
        )

    def _scan_complete(self, batch: HygieneBatch) -> None:
        self.batch = batch
        self.busy = False
        self.sub_title = "Review suspicious metadata"
        self._rebuild_rows(select_all=True)
        self._rebuild_tree()

    @on(Tree.NodeHighlighted, "#hygiene-tree")
    def node_highlighted(self, event: Tree.NodeHighlighted[int | Path]) -> None:
        if event.node.data is not None:
            self._update_inspector(event.node.data)
            self.query_one("#inspector-scroll", VerticalScroll).scroll_home(animate=False)

    @on(Tree.NodeSelected, "#hygiene-tree")
    def node_selected(self, event: Tree.NodeSelected[int | Path]) -> None:
        if isinstance(event.node.data, Path):
            event.node.toggle()
        elif event.node.data is not None and not self.has_class("details-open"):
            self.action_toggle_details()

    def _update_inspector(self, index: int | Path) -> None:
        if isinstance(index, Path):
            node = self._track_nodes.get(index)
            if node is None:
                return
            indices = [child.data for child in node.children if isinstance(child.data, int)]
            checked = len(set(indices) & self.selected)
            self.query_one("#inspector", Static).update(
                "\n".join(
                    (
                        f"{checked} of {len(indices)} fixes included in cleanup",
                        "Space includes or excludes all fixes for this track.",
                        "Enter or ←/→ collapses or expands its fixes.",
                        "",
                        *(self._finding_label(item).plain for item in indices),
                        "",
                        str(index),
                    )
                )
            )
            return
        if index >= len(self.rows):
            return
        row = self.rows[index]
        if row.failure is not None:
            self.query_one("#inspector", Static).update(
                "\n".join(
                    (
                        row.path.name,
                        str(row.path.parent),
                        "",
                        "Inspection error",
                        row.failure.description,
                        "",
                        "This file was not classified as clean and cannot enter a cleanup write.",
                    )
                )
            )
            return
        track = row.track
        finding = row.finding
        assert track is not None
        assert finding is not None
        after = "\n".join(finding.after) if finding.after else "Remove this tag"
        current = "\n".join(value or "(empty)" for value in finding.before) or "(empty tag)"
        self.query_one("#inspector", Static).update(
            "\n".join(
                (
                    "Included in cleanup" if index in self.selected else "Excluded from cleanup",
                    f"Tag: {finding.label}",
                    f"Reason: {finding.reason_text}",
                    f"After cleanup: {after}",
                    "",
                    "Current value",
                    current,
                    "",
                    str(track.path),
                    f"Container: {track.metadata_format}",
                    "Other metadata is preserved.",
                )
            )
        )

    def action_toggle_finding(self) -> None:
        if self.busy:
            return
        item = self._current_item()
        if item is None:
            return
        if isinstance(item, Path):
            eligible = {
                child.data
                for child in self._track_nodes[item].children
                if isinstance(child.data, int)
            }
        elif self.rows[item].is_selectable:
            eligible = {item}
        else:
            self.notify(
                "This file could not be inspected and cannot be selected.", severity="warning"
            )
            return
        if eligible.issubset(self.selected):
            self.selected.difference_update(eligible)
        else:
            self.selected.update(eligible)
        self._refresh_selection(eligible)

    def _refresh_selection(self, indices: set[int]) -> None:
        for index in indices:
            self._finding_nodes[index].set_label(self._finding_label(index))
        for path in {self.rows[index].path for index in indices}:
            self._track_nodes[path].set_label(self._track_label(path))
        item = self._current_item()
        if item is not None:
            self._update_inspector(item)
        self._update_status()

    def action_toggle_all(self) -> None:
        if self.busy or not self.rows:
            return
        eligible = set(self._finding_nodes)
        if not eligible:
            return
        self.selected = set() if eligible.issubset(self.selected) else eligible
        self._refresh_selection(eligible)

    def action_toggle_details(self) -> None:
        visible = not self.has_class("details-open")
        self.set_class(visible, "details-open")
        if visible:
            item = self._current_item()
            if item is not None:
                self._update_inspector(item)
            self.call_after_refresh(self.query_one("#inspector-scroll", VerticalScroll).focus)
        else:
            self.query_one(HygieneTree).focus()

    def _selected_plans(self) -> tuple[HygienePlan, ...]:
        grouped: dict[Path, list[HygieneFinding]] = defaultdict(list)
        tracks: dict[Path, HygieneTrack] = {}
        for index in sorted(self.selected):
            row = self.rows[index]
            track = row.track
            finding = row.finding
            assert track is not None
            assert finding is not None
            tracks[track.path] = track
            grouped[track.path].append(finding)
        return tuple(
            plan_hygiene_track(tracks[path], findings) for path, findings in grouped.items()
        )

    def action_write(self) -> None:
        if self.busy:
            return
        plans = self._selected_plans()
        if not plans:
            self.notify("Check at least one hygiene suggestion before cleaning.")
            return
        self.busy = True
        self._pending = plans
        self._update_status("Checking files and staged tag values…")
        self._preflight_for_confirmation(plans)

    @work(thread=True, exclusive=True, group="hygiene-preflight", exit_on_error=False)
    def _preflight_for_confirmation(self, plans: tuple[HygienePlan, ...]) -> None:
        try:
            prepared = preflight_hygiene(plans)
            summary = summarize_hygiene(plans)
        except Exception as error:
            self.call_from_thread(self._failed, "Hygiene check failed", str(error))
            return
        self.call_from_thread(self._show_confirmation, prepared, summary)

    def _show_confirmation(
        self,
        prepared: tuple[PreparedHygiene, ...],
        summary: HygieneSummary,
    ) -> None:
        self._pending_prepared = prepared
        self.push_screen(ConfirmWriteScreen(summary), self._confirmed)

    def _confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.busy = False
            self._pending = ()
            self._pending_prepared = ()
            self._update_status("Cleanup cancelled; nothing changed")
            return
        self._update_status("Rechecking files before cleanup…")
        self._apply_pending()

    @work(thread=True, exclusive=True, group="hygiene-write", exit_on_error=False)
    def _apply_pending(self) -> None:
        recorder = BatchRecorder(self.journal)
        try:
            prepared = preflight_hygiene(self._pending)
            written = apply_hygiene(
                prepared,
                on_progress=self._progress_from_worker,
                on_write=recorder,
            )
            refreshed = inspect_hygiene_paths(self.paths)
        except PartialHygieneWriteError as error:
            refreshed = inspect_hygiene_paths(self.paths)
            self.call_from_thread(
                self._partly_failed,
                str(error),
                refreshed,
                recorder.error,
            )
            return
        except Exception as error:
            self.call_from_thread(self._failed, "Cleanup failed", str(error))
            return
        self.call_from_thread(self._complete, written, refreshed, recorder)

    def _progress_from_worker(self, completed: int, total: int, path: Path) -> None:
        self.call_from_thread(
            self._update_status,
            f"Cleaning {completed} of {total}: {path.name}",
        )

    def _complete(
        self,
        written: int,
        refreshed: HygieneBatch,
        recorder: BatchRecorder,
    ) -> None:
        self.batch = refreshed
        self._rebuild_rows(select_all=False)
        self.busy = False
        self._pending = ()
        self._pending_prepared = ()
        message = f"Cleaned and verified {written} file{'s' if written != 1 else ''}."
        if recorder.error is not None:
            self.notify(recorder.error, severity="warning", timeout=8)
        elif recorder.recorded:
            message += f" Undo with: settag undo {recorder.batch_id}"
        self._rebuild_tree(message=message)
        self.notify(message, title="Hygiene complete", timeout=7)

    def _partly_failed(
        self,
        message: str,
        refreshed: HygieneBatch,
        journal_error: str | None,
    ) -> None:
        self.batch = refreshed
        self._rebuild_rows(select_all=False)
        self.busy = False
        self._pending = ()
        self._pending_prepared = ()
        self._rebuild_tree()
        if journal_error is not None:
            self.notify(journal_error, severity="warning", timeout=8)
        self.push_screen(ErrorScreen("Cleanup stopped", message))

    def _failed(self, title: str, message: str) -> None:
        self.busy = False
        self._pending = ()
        self._pending_prepared = ()
        self._update_status("Nothing was changed")
        self.push_screen(ErrorScreen(title, message))

    async def action_quit(self) -> None:
        if self.busy:
            self.notify("A safety check or cleanup write is in progress.", severity="warning")
            return
        self.exit(TuiOutcome(0, "Hygiene review closed."))
