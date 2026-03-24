# Copyright 2026 Marimo. All rights reserved.
"""Canonical notebook document model.

``NotebookDocument`` maintains an ordered list of ``CellMeta`` entries and
a ``LoroDoc`` that owns all cell source text as ``LoroText`` containers.
``NotebookCell`` is a read-only snapshot materialized on access — it is
never stored internally.

The document applies ``Transaction``s atomically.  It is a pure state
machine — no IO, no notifications, no kernel interaction.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

from loro import LoroDoc, LoroText
from msgspec.structs import replace as structs_replace

from marimo._notebook._loro import create_doc, create_text

import msgspec

from marimo._ast.cell import CellConfig
from marimo._notebook.ops import (
    CreateCell,
    DeleteCell,
    MoveCell,
    Op,
    ReorderCells,
    SetCode,
    SetConfig,
    SetName,
    Transaction,
)
from marimo._types.ids import CellId_t


class NotebookCell(msgspec.Struct, frozen=True):
    """Read-only snapshot of a cell, materialized from CellMeta + LoroText.

    This is never stored by the document — ``get_cell()`` and ``.cells``
    construct fresh instances each time.
    """

    id: CellId_t
    code: str
    name: str
    config: CellConfig


class CellMeta:
    """Mutable metadata for a cell.  Owned by the document internally.

    Does *not* hold code — that lives in the ``LoroDoc``.
    """

    __slots__ = ("id", "name", "config")

    def __init__(
        self, id: CellId_t, name: str, config: CellConfig
    ) -> None:
        self.id = id
        self.name = name
        self.config = config


class NotebookDocument:
    """Ordered collection of cells with transactional updates.

    Cell text is owned by a ``LoroDoc`` (one ``LoroText`` per cell under
    ``LoroMap("codes")``).  Structural metadata (name, config, ordering)
    is stored in ``_cell_metas``.

    Usage::

        from loro import LoroDoc
        doc = NotebookDocument(LoroDoc())
        doc.add_cell(CellId_t("a"), code="x = 1", name="__", config=CellConfig())
        tx = Transaction(
            ops=(SetCode(CellId_t("a"), "x = 2"),), source="kernel"
        )
        applied = doc.apply(tx)
        assert applied.version == 1
        assert doc.get_cell(CellId_t("a")).code == "x = 2"
    """

    def __init__(self, loro_doc: LoroDoc) -> None:
        self._loro_doc = loro_doc
        self._codes_map = loro_doc.get_map("codes")
        self._cell_metas: list[CellMeta] = []
        self._version: int = 0

    @classmethod
    def from_cells(cls, cells: Iterable[NotebookCell]) -> NotebookDocument:
        """Build a document from ``NotebookCell`` snapshots.

        Creates a fresh ``LoroDoc`` populated from the snapshot data.
        Used at the kernel-process boundary where cells arrive as
        serialized structs and need to be reconstructed into a live
        document.
        """
        doc = cls(create_doc())
        for c in cells:
            doc.add_cell(
                cell_id=c.id, code=c.code, name=c.name, config=c.config
            )
        doc._loro_doc.commit()
        return doc

    @property
    def loro_doc(self) -> LoroDoc:
        """The underlying Loro document owning cell text."""
        return self._loro_doc

    # ------------------------------------------------------------------
    # Bootstrap — populate the document from an external source
    # ------------------------------------------------------------------

    def add_cell(
        self,
        cell_id: CellId_t,
        code: str,
        name: str,
        config: CellConfig,
    ) -> None:
        """Append a cell during initial document construction.

        This is *not* a transaction — it is used at session init to
        populate the document from a ``CellManager``.
        """
        text = create_text()
        text.insert(0, code)
        self._codes_map.insert_container(cell_id, text)
        self._cell_metas.append(CellMeta(id=cell_id, name=name, config=config))

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def cells(self) -> list[NotebookCell]:
        """Materialize and return a snapshot list of all cells."""
        return [self._snapshot(m) for m in self._cell_metas]

    @property
    def cell_ids(self) -> list[CellId_t]:
        """Cell IDs in document order."""
        return [m.id for m in self._cell_metas]

    @property
    def version(self) -> int:
        return self._version

    def get_cell(self, cell_id: CellId_t) -> NotebookCell:
        """Lookup by ID. Raises ``KeyError`` if not found."""
        return self._snapshot(self._find_meta(cell_id))

    def get(self, cell_id: CellId_t) -> NotebookCell | None:
        """Lookup by ID, returning ``None`` if not found."""
        for m in self._cell_metas:
            if m.id == cell_id:
                return self._snapshot(m)
        return None

    def __contains__(self, cell_id: object) -> bool:
        return any(m.id == cell_id for m in self._cell_metas)

    def __len__(self) -> int:
        return len(self._cell_metas)

    def __iter__(self) -> Iterator[CellId_t]:
        return (m.id for m in self._cell_metas)

    # ------------------------------------------------------------------
    # Transaction application
    # ------------------------------------------------------------------

    def apply(self, tx: Transaction) -> Transaction:
        """Validate and apply *tx*, return it with ``version`` assigned.

        Raises ``ValueError`` for validation failures and ``KeyError``
        when an op references a non-existent cell.
        """
        if not tx.ops:
            return structs_replace(tx, version=self._version)

        _validate(tx.ops, self._cell_metas)

        for op in tx.ops:
            self._apply_op(op)

        self._version += 1
        return structs_replace(tx, version=self._version)

    def _apply_op(self, op: Op) -> None:
        if isinstance(op, CreateCell):
            # Create LoroText in the shared doc
            text = create_text()
            text.insert(0, op.code)
            self._codes_map.insert_container(op.cell_id, text)

            meta = CellMeta(id=op.cell_id, name=op.name, config=op.config)
            if op.after is not None:
                idx = self._find_index(op.after)
                self._cell_metas.insert(idx + 1, meta)
            elif op.before is not None:
                idx = self._find_index(op.before)
                self._cell_metas.insert(idx, meta)
            else:
                self._cell_metas.append(meta)

        elif isinstance(op, DeleteCell):
            idx = self._find_index(op.cell_id)
            del self._cell_metas[idx]
            self._codes_map.delete(op.cell_id)

        elif isinstance(op, MoveCell):
            idx = self._find_index(op.cell_id)
            meta = self._cell_metas.pop(idx)
            if op.after is not None:
                target = self._find_index(op.after)
                self._cell_metas.insert(target + 1, meta)
            elif op.before is not None:
                target = self._find_index(op.before)
                self._cell_metas.insert(target, meta)
            else:
                raise ValueError("MoveCell requires 'before' or 'after'")

        elif isinstance(op, ReorderCells):
            by_id = {m.id: m for m in self._cell_metas}
            seen: set[CellId_t] = set()
            reordered: list[CellMeta] = []
            for cid in op.cell_ids:
                if cid in by_id and cid not in seen:
                    reordered.append(by_id[cid])
                    seen.add(cid)
            for m in self._cell_metas:
                if m.id not in seen:
                    reordered.append(m)
            self._cell_metas = reordered

        elif isinstance(op, SetCode):
            # Verify cell exists
            self._find_meta(op.cell_id)
            # Full replace in Loro
            text = self._get_loro_text(op.cell_id)
            if text.len_unicode > 0:
                text.delete(0, text.len_unicode)
            if op.code:
                text.insert(0, op.code)

        elif isinstance(op, SetName):
            self._find_meta(op.cell_id).name = op.name

        elif isinstance(op, SetConfig):
            meta = self._find_meta(op.cell_id)
            meta.config = CellConfig(
                column=op.column
                if op.column is not None
                else meta.config.column,
                disabled=op.disabled
                if op.disabled is not None
                else meta.config.disabled,
                hide_code=op.hide_code
                if op.hide_code is not None
                else meta.config.hide_code,
            )
        else:
            raise TypeError(f"Unknown op type: {type(op)!r}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _snapshot(self, meta: CellMeta) -> NotebookCell:
        """Build a read-only ``NotebookCell`` from metadata + Loro text."""
        code = self._get_loro_text(meta.id).to_string()
        return NotebookCell(
            id=meta.id, code=code, name=meta.name, config=meta.config
        )

    def _get_loro_text(self, cell_id: CellId_t) -> LoroText:
        """Return the ``LoroText`` for *cell_id*."""
        val = self._codes_map.get(cell_id)
        if val is None:
            raise KeyError(f"No LoroText for cell {cell_id!r}")
        container = val.container  # type: ignore[union-attr,attr-defined]
        assert isinstance(container, LoroText)
        return container

    def _find_index(self, cell_id: CellId_t) -> int:
        for i, m in enumerate(self._cell_metas):
            if m.id == cell_id:
                return i
        raise KeyError(f"Cell {cell_id!r} not found in document")

    def _find_meta(self, cell_id: CellId_t) -> CellMeta:
        for m in self._cell_metas:
            if m.id == cell_id:
                return m
        raise KeyError(f"Cell {cell_id!r} not found in document")

    def __repr__(self) -> str:
        lines = [f"NotebookDocument({len(self._cell_metas)} cells):"]
        for i, m in enumerate(self._cell_metas):
            code_preview = self._get_loro_text(m.id).to_string()[:40].replace(
                "\n", "\\n"
            )
            lines.append(f"  {i}: {m.id} {code_preview!r}")
        return "\n".join(lines)


# ------------------------------------------------------------------
# Context variable
# ------------------------------------------------------------------

#: Document snapshot for the current scratchpad execution. Set by the
#: kernel before running code_mode so ``AsyncCodeModeContext`` can read
#: cell ordering, code, names, and configs without the kernel carrying
#: mutable document state.
_current_document: ContextVar[NotebookDocument | None] = ContextVar(
    "_current_document", default=None
)


def get_current_document() -> NotebookDocument | None:
    """Return the document for the current execution, if any."""
    return _current_document.get()


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


def _validate(ops: tuple[Op, ...], metas: list[CellMeta]) -> None:
    """Check for conflicting operations. Raises ``ValueError``."""
    existing_ids = {m.id for m in metas}
    created: set[CellId_t] = set()
    deleted: set[CellId_t] = set()
    updated: set[CellId_t] = set()
    moved: set[CellId_t] = set()

    for op in ops:
        if isinstance(op, CreateCell):
            if op.cell_id in existing_ids or op.cell_id in created:
                raise ValueError(f"Cell {op.cell_id!r} already exists")
            if op.before is not None and op.after is not None:
                raise ValueError(
                    "CreateCell cannot specify both 'before' and 'after'"
                )
            created.add(op.cell_id)

        elif isinstance(op, DeleteCell):
            if op.cell_id in deleted:
                raise ValueError(
                    f"Cell {op.cell_id!r} is deleted more than once"
                )
            if op.cell_id in updated:
                raise ValueError(
                    f"Cannot delete cell {op.cell_id!r} that is also "
                    f"updated in the same transaction"
                )
            if op.cell_id in moved:
                raise ValueError(
                    f"Cannot delete cell {op.cell_id!r} that is also "
                    f"moved in the same transaction"
                )
            deleted.add(op.cell_id)

        elif isinstance(op, MoveCell):
            if op.cell_id in deleted:
                raise ValueError(
                    f"Cannot move cell {op.cell_id!r} that is also "
                    f"deleted in the same transaction"
                )
            if op.before is not None and op.after is not None:
                raise ValueError(
                    "MoveCell cannot specify both 'before' and 'after'"
                )
            if op.before is None and op.after is None:
                raise ValueError("MoveCell requires 'before' or 'after'")
            moved.add(op.cell_id)

        elif isinstance(op, ReorderCells):
            pass  # No conflicts — replaces full ordering

        elif isinstance(op, (SetCode, SetName, SetConfig)):
            if op.cell_id in deleted:
                raise ValueError(
                    f"Cannot update cell {op.cell_id!r} that is also "
                    f"deleted in the same transaction"
                )
            updated.add(op.cell_id)

        else:
            raise TypeError(f"Unknown op type: {type(op)!r}")
