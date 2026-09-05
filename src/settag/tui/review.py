"""A review tree: whole-track write choices with read-only change and evidence rows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from settag.review_evidence import describe_evidence
from settag.tags import read_task_provenance, task_evidence_from_owned
from settag.tui.entries import TASK_LABELS, TrackEntry, suggested_label
from settag.tui.table import RowContext

NodeKey = tuple[int, str]


@dataclass(frozen=True)
class ReviewNode:
    key: NodeKey
    label: str
    children: tuple[ReviewNode, ...] = ()
    expanded: bool = False


def review_track(index: int, entry: TrackEntry, selected: bool, context: RowContext) -> ReviewNode:
    """Describe a track without making its candidate scores independently writable."""
    if entry.analysis_error is not None:
        return ReviewNode(
            (index, "track"),
            f"! {entry.path.name} · Enrichment failed",
            (ReviewNode((index, "error"), entry.analysis_error.description),),
            expanded=True,
        )
    plan = entry.plan
    if plan is None:
        return ReviewNode((index, "track"), f"{entry.path.name} · No enrichment result")

    inclusion = "Included in write" if selected else "Excluded from write"
    marker = "[x]" if selected else "[ ]"
    if not entry.has_changes:
        marker, inclusion = "—", "No changes to write"
    review = describe_evidence(plan)
    by_task = task_evidence_from_owned(plan.desired)
    provenance = read_task_provenance(plan.desired)
    candidates: list[ReviewNode] = []
    for task in context.tasks:
        predictions = by_task.get(task, ())
        chosen = context.select_for_review(predictions)
        if chosen:
            candidates.extend(
                ReviewNode(
                    (index, f"candidate:{task}:{rank}"),
                    f"{TASK_LABELS[task]}: {suggested_label((prediction,))}"
                    f" · {prediction.score:.3f}",
                )
                for rank, prediction in enumerate(chosen)
            )
        else:
            state = (
                "No candidate met the cutoff"
                if predictions
                else "No ranked evidence"
                if task in provenance
                else "Not analyzed"
            )
            candidates.append(
                ReviewNode((index, f"candidate:{task}"), f"{TASK_LABELS[task]}: {state}")
            )

    return ReviewNode(
        (index, "track"),
        f"{marker} {entry.path.name} · {inclusion}",
        (
            ReviewNode((index, "recommendation"), f"Recommendation: {review.recommendation}"),
            ReviewNode(
                (index, "recommendation-source"), f"Based on: {review.recommendation_source}"
            ),
            ReviewNode((index, "genre"), f"Current file tag: {review.current_genre}"),
            ReviewNode(
                (index, "beatport"),
                review.catalog_title,
                tuple(
                    ReviewNode((index, f"source:{number}"), detail)
                    for number, detail in enumerate(review.catalog_details)
                ),
                expanded=True,
            ),
            ReviewNode(
                (index, "candidates"),
                "Audio models · predictions",
                (
                    *(
                        ReviewNode((index, f"model-note:{number}"), detail)
                        for number, detail in enumerate(review.model_details)
                    ),
                    *candidates,
                ),
            ),
            ReviewNode(
                (index, "changes"),
                "Changes to save" if selected else "Changes if included",
                tuple(
                    ReviewNode((index, f"change:{number}"), detail)
                    for number, detail in enumerate(review.changes)
                ),
                expanded=True,
            ),
            *(
                ReviewNode((index, f"notice:{number}"), detail)
                for number, detail in enumerate(review.notices)
            ),
        ),
        expanded=True,
    )


class ReviewTree(Tree[NodeKey]):
    """Keep stable nodes, expansion, and navigation as background results arrive."""

    BINDINGS = [
        Binding("space", "app.toggle_track", "Toggle track"),
        Binding("enter", "select_cursor", "Expand/Details"),
        Binding("left", "collapse_or_parent", "Collapse", show=False),
        Binding("right", "expand_or_child", "Expand", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("Reviewed tracks", id="review-tree")
        self.show_root = False
        self.auto_expand = False
        self.nodes: dict[NodeKey, TreeNode[NodeKey]] = {}

    @property
    def current_index(self) -> int | None:
        node = self.cursor_node
        return node.data[0] if node is not None and node.data is not None else None

    def sync(
        self,
        entries: Sequence[TrackEntry],
        indices: Sequence[int],
        selected: set[int],
        context: RowContext,
        *,
        preferred_index: int | None = None,
        preserve_view: bool = False,
    ) -> None:
        previous = self.cursor_node.data if self.cursor_node is not None else None
        scroll_x, scroll_y = self.scroll_x, self.scroll_y
        specs = tuple(
            review_track(index, entries[index], index in selected, context) for index in indices
        )
        nodes: dict[NodeKey, TreeNode[NodeKey]] = {}
        self._sync_nodes(self.root, specs, nodes)
        self.nodes = nodes
        if not specs:
            self.root.add_leaf("No tracks ready to review.")
        self.root.expand()
        target = self.nodes.get(previous) if previous is not None else None
        if target is None and preferred_index is not None:
            target = self.nodes.get((preferred_index, "track"))
        if target is None:
            target = self.root.children[0]
        self.call_after_refresh(self._restore_cursor, target, preserve_view, scroll_x, scroll_y)

    def _sync_nodes(
        self,
        parent: TreeNode[NodeKey],
        specs: Sequence[ReviewNode],
        nodes: dict[NodeKey, TreeNode[NodeKey]],
    ) -> None:
        wanted = {spec.key for spec in specs}
        for child in list(parent.children):
            if child.data not in wanted:
                child.remove()
        existing = {child.data: child for child in parent.children}
        for position, spec in enumerate(specs):
            node = existing.get(spec.key)
            label = Text(spec.label, style="bold" if spec.key[1] == "track" else "")
            if node is None:
                node = parent.add(
                    label,
                    data=spec.key,
                    before=position,
                    expand=spec.expanded,
                    allow_expand=bool(spec.children),
                )
            else:
                if node.label != label:
                    node.set_label(label)
                node.allow_expand = bool(spec.children)
            nodes[spec.key] = node
            self._sync_nodes(node, spec.children, nodes)

    def update_track(
        self, index: int, entry: TrackEntry, selected: bool, context: RowContext
    ) -> None:
        """Refresh one edit or checkbox without reparsing every track's evidence."""
        track = self.nodes[(index, "track")]
        previous = self.cursor_node.data if self.cursor_node is not None else None
        scroll_x, scroll_y = self.scroll_x, self.scroll_y
        spec = review_track(index, entry, selected, context)
        track.set_label(Text(spec.label, style="bold"))
        track.allow_expand = bool(spec.children)
        nodes = {spec.key: track}
        self._sync_nodes(track, spec.children, nodes)
        for key in tuple(self.nodes):
            if key[0] == index and key not in nodes:
                del self.nodes[key]
        self.nodes.update(nodes)
        target = self.nodes.get(previous, track) if previous is not None else track
        self.call_after_refresh(self._restore_cursor, target, True, scroll_x, scroll_y)

    def _restore_cursor(
        self, node: TreeNode[NodeKey], preserve_view: bool, scroll_x: float, scroll_y: float
    ) -> None:
        if preserve_view:
            with self.prevent(Tree.NodeHighlighted):
                self.move_cursor(node)
            self.scroll_to(x=scroll_x, y=scroll_y, animate=False)
        else:
            self.move_cursor(node)

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
