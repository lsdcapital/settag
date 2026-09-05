"""The app stylesheet.

Kept apart from the app so behaviour and appearance can be read and changed
independently; ``SetTagApp.CSS`` simply points at it.
"""

from __future__ import annotations

APP_CSS = """
Screen {
    background: #0b0f0e;
    color: #eef2f1;
}

Header {
    background: #16201e;
    color: #eef2f1;
}

Footer {
    background: #16201e;
    color: #c3cecb;
}

Footer > .footer--highlight,
Footer > .footer--key {
    background: #d0794f;
    color: #1f0e05;
}

#loading {
    align: center middle;
    height: 1fr;
    padding: 2 6;
}

#loading-title {
    text-style: bold;
    color: #eef2f1;
    margin-bottom: 1;
}

#loading-path {
    color: #8ea09b;
    margin-bottom: 1;
}

#metadata-progress {
    width: 72;
    max-width: 90%;
    margin-top: 1;
}

#main {
    display: none;
    height: 1fr;
}

#hygiene-main {
    height: 1fr;
}

#context {
    height: 3;
    padding: 1 2 0 2;
    color: #8ea09b;
    background: #0f1413;
}

#analysis-activity {
    display: none;
    height: 6;
    padding: 1 2 0 2;
    background: #16201e;
}

#library-filters {
    height: 1;
    padding: 0 2;
    color: #c3cecb;
    background: #16201e;
}

#library-empty {
    display: none;
    height: auto;
    padding: 1;
    color: #c3cecb;
}

#analysis-activity-title {
    height: 1;
    text-style: bold;
    color: #eef2f1;
}

#analysis-activity-file {
    height: 1;
    color: #c3cecb;
}

#analysis-progress {
    width: 100%;
    height: 1;
    margin-top: 1;
}

#workspace {
    height: 1fr;
}

#tracks-pane {
    width: 2fr;
    min-width: 48;
    padding: 0 1 1 1;
}

#inspector-pane {
    display: none;
    width: 1fr;
    min-width: 34;
    padding: 0 2 1 1;
    background: #0f1413;
}

SetTagApp.details-open #inspector-pane {
    display: block;
}

HygieneApp.details-open #inspector-pane {
    display: block;
}

.section-title {
    height: 2;
    padding: 0 1;
    text-style: bold;
    color: #eef2f1;
}

DataTable {
    height: 1fr;
    background: #111716;
    color: #c3cecb;
    scrollbar-color: #3a4744;
    scrollbar-color-hover: #8ea09b;
    scrollbar-color-active: #d0794f;
}

DataTable > .datatable--header {
    background: #16201e;
    color: #eef2f1;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: #d0794f;
    color: #1f0e05;
    text-style: bold;
}

#review-tree {
    display: none;
}

#hygiene-tree,
#review-tree {
    height: 1fr;
    background: #111716;
    background-tint: transparent;
    color: #c3cecb;
    scrollbar-color: #3a4744;
    scrollbar-color-hover: #8ea09b;
    scrollbar-color-active: #d0794f;
}

#hygiene-tree > .tree--guides,
#review-tree > .tree--guides {
    color: #3a4744;
}

#hygiene-tree > .tree--cursor,
#review-tree > .tree--cursor {
    background: #16201e;
    color: #eef2f1;
}

#hygiene-tree:focus > .tree--cursor,
#review-tree:focus > .tree--cursor {
    background: #d0794f;
    color: #1f0e05;
    text-style: bold;
}

#inspector-scroll {
    height: 1fr;
    padding: 0 1;
    color: #c3cecb;
    overflow-y: auto;
    border-left: solid #3a4744;
    scrollbar-color: #3a4744;
    scrollbar-color-hover: #8ea09b;
    scrollbar-color-active: #d0794f;
}

#inspector-scroll:focus {
    border-left: solid #d0794f;
}

#inspector {
    width: 1fr;
}

#status {
    height: 3;
    padding: 1 2;
    background: #16201e;
    color: #c3cecb;
}

ModalScreen {
    align: center middle;
    background: #000000 55%;
}

#genre-dialog,
#confirm-dialog,
#error-dialog {
    width: 64;
    max-width: 92%;
    height: auto;
    max-height: 86%;
    padding: 1 2;
    background: #16201e;
    border: solid #3a4744;
}

#confirm-dialog {
    width: 76;
}

#undo-dialog {
    width: 88;
    max-width: 92%;
    height: auto;
    max-height: 86%;
    padding: 1 2;
    background: #16201e;
    border: solid #3a4744;
}

#undo-table {
    height: auto;
    max-height: 14;
    margin-bottom: 1;
    background: #0f1413;
}

#dialog-title {
    text-style: bold;
    color: #eef2f1;
    margin-bottom: 1;
}

#dialog-help,
#dialog-suggestion {
    color: #8ea09b;
    margin-bottom: 1;
}

#genre-input {
    margin-bottom: 1;
    border: tall #3a4744;
}

#genre-input:focus {
    border: tall #d0794f;
}

#confirm-summary,
#error-message {
    margin-bottom: 1;
    color: #eef2f1;
    max-height: 16;
    overflow-y: auto;
}

#confirm-summary {
    max-height: 20;
}

.dialog-actions {
    height: 3;
    align-horizontal: right;
}

.dialog-actions Button {
    margin-left: 1;
}

GenreEditScreen.narrow .dialog-actions {
    height: 9;
    layout: vertical;
    align-horizontal: right;
}

#confirm-dialog #cancel,
#undo-dialog #cancel {
    background: #25302d;
    color: #c3cecb;
}

#confirm-dialog #cancel:focus,
#undo-dialog #cancel:focus {
    background: #3a4744;
    color: #eef2f1;
}

#confirm-dialog #confirm,
#confirm-dialog #confirm:focus,
#undo-dialog #confirm,
#undo-dialog #confirm:focus {
    background: #d0794f;
    color: #1f0e05;
    text-style: bold;
}

Button.-primary {
    background: #d0794f;
    color: #1f0e05;
}

SetTagApp.narrow #workspace {
    layout: vertical;
}

HygieneApp.narrow #workspace {
    layout: vertical;
}

SetTagApp.narrow #tracks-pane,
SetTagApp.narrow #inspector-pane {
    width: 1fr;
    min-width: 0;
}

HygieneApp.narrow #tracks-pane,
HygieneApp.narrow #inspector-pane {
    width: 1fr;
    min-width: 0;
}

SetTagApp.narrow #tracks-pane {
    height: 3fr;
}

HygieneApp.narrow #tracks-pane {
    height: 3fr;
}

SetTagApp.narrow #inspector-pane {
    height: 2fr;
    padding: 0 1 1 1;
}

HygieneApp.narrow #inspector-pane {
    height: 2fr;
    padding: 0 1 1 1;
}
"""
