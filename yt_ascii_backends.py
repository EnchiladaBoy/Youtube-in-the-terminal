"""Rendering-backend capability contracts shared across the pipeline.

This module contains no NumPy or terminal-composition implementation.  It is
the single selection boundary used by frame contexts, visual effects, the ANSI
renderer, diagnostics, and qualification tooling.  A future bitmap protocol
can therefore declare its capabilities here and consume the same post-effect
RGB frame without teaching individual effects its name.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class RenderBackendSpec:
    """Stable capabilities for one terminal frame-composition backend."""

    name: str
    protocol: str
    source_rows_per_cell: int
    unicode_dependent: bool
    supports_cell_plane: bool
    requires_color: bool
    portable: bool


RENDER_BACKENDS = MappingProxyType({
    "chars": RenderBackendSpec(
        name="chars",
        protocol="ansi",
        source_rows_per_cell=1,
        unicode_dependent=False,
        supports_cell_plane=True,
        requires_color=False,
        portable=True,
    ),
    "cells": RenderBackendSpec(
        name="cells",
        protocol="ansi",
        source_rows_per_cell=1,
        unicode_dependent=False,
        supports_cell_plane=False,
        requires_color=True,
        portable=True,
    ),
    "half-block": RenderBackendSpec(
        name="half-block",
        protocol="ansi",
        source_rows_per_cell=2,
        unicode_dependent=True,
        supports_cell_plane=False,
        requires_color=True,
        portable=True,
    ),
})


def get_render_backend(name):
    """Return the capability record for a canonical rendering backend."""
    if not isinstance(name, str):
        raise TypeError("render mode must be a string")
    try:
        return RENDER_BACKENDS[name]
    except KeyError:
        choices = ", ".join(RENDER_BACKENDS)
        raise ValueError(
            f"unknown render mode {name!r}; choose from: {choices}"
        ) from None


def render_modes_for_frame_kind(frame_kind):
    """Return backends that can compose ``rgb`` or structured ``text`` frames."""
    if frame_kind == "rgb":
        return tuple(RENDER_BACKENDS)
    if frame_kind == "text":
        return tuple(
            name
            for name, backend in RENDER_BACKENDS.items()
            if backend.supports_cell_plane
        )
    raise ValueError("frame kind must be 'rgb' or 'text'")
