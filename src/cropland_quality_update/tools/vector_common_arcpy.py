"""Shared ArcPy vector-source helpers used by the workflow tools."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, END, ttk

try:
    import arcpy
except ImportError as exc:  # pragma: no cover - shown to user at runtime
    arcpy = None
    ARCPY_IMPORT_ERROR = exc
else:
    ARCPY_IMPORT_ERROR = None


GEOMETRY_FIELD_NAMES = {"shape", "shape_length", "shape_area"}
SKIP_FIELD_TYPES = {"OID", "Geometry", "Blob", "Raster", "GUID", "GlobalID", "XML"}


@dataclass(frozen=True)
class VectorSource:
    kind: str  # shp | gdb
    source_path: Path
    layer_name: str | None
    display_name: str


@dataclass(frozen=True)
class ProjectionInfo:
    source: VectorSource
    spatial_reference: object | None
    message: str


@dataclass(frozen=True)
class PreparedSource:
    path: str
    source: VectorSource


def require_arcpy() -> None:
    if arcpy is None:
        raise RuntimeError(f"缺少 arcpy。请使用 ArcGIS Pro Python 环境运行：{ARCPY_IMPORT_ERROR}")


def make_vector_source(kind: str, source_path: Path, layer_name: str | None = None) -> VectorSource:
    source_path = source_path.resolve()
    if kind == "shp":
        shp_path = source_path if source_path.suffix.lower() == ".shp" else source_path.with_suffix(".shp")
        return VectorSource("shp", shp_path, None, str(shp_path))
    if kind == "gdb":
        if not layer_name:
            raise ValueError("gdb source requires layer_name")
        return VectorSource("gdb", source_path, layer_name, f"{source_path}\\{layer_name}")
    raise ValueError(f"unsupported source kind: {kind}")


def source_label(source: VectorSource) -> str:
    return source.display_name


def source_path_for_log(source: VectorSource) -> str:
    return source.display_name


def source_dataset_path(source: VectorSource) -> str:
    if source.kind == "gdb":
        return str(source.source_path / str(source.layer_name))
    return str(source.source_path)


def is_gdb_path(path: Path) -> bool:
    return path.suffix.lower() == ".gdb" and path.is_dir()


def output_dataset_path(output_kind: str, output_path: Path, output_feature_name: str | None) -> Path:
    return output_path if output_kind == "shp" else output_path / str(output_feature_name)


def list_shp_sources(folder: Path) -> list[VectorSource]:
    return [make_vector_source("shp", path) for path in sorted(folder.rglob("*.shp"))]


def iter_gdb_paths(root: Path) -> list[Path]:
    root = root.resolve()
    gdb_paths: list[Path] = []
    if is_gdb_path(root):
        gdb_paths.append(root)
    if root.is_dir():
        known = {path.resolve() for path in gdb_paths}
        for candidate in root.rglob("*.gdb"):
            resolved = candidate.resolve()
            if candidate.is_dir() and resolved not in known:
                gdb_paths.append(candidate)
                known.add(resolved)
    return gdb_paths


def list_gdb_polygon_layers(gdb_path: Path) -> list[VectorSource]:
    require_arcpy()
    previous_workspace = arcpy.env.workspace
    arcpy.env.workspace = str(gdb_path)
    try:
        sources: list[VectorSource] = []
        for feature_class in arcpy.ListFeatureClasses(feature_type="Polygon") or []:
            sources.append(make_vector_source("gdb", gdb_path, feature_class))
        for dataset in arcpy.ListDatasets(feature_type="Feature") or []:
            for feature_class in arcpy.ListFeatureClasses(feature_dataset=dataset, feature_type="Polygon") or []:
                sources.append(make_vector_source("gdb", gdb_path, f"{dataset}\\{feature_class}"))
        return sources
    finally:
        arcpy.env.workspace = previous_workspace


def discover_sources_in_folder_with_errors(folder: Path) -> tuple[list[VectorSource], list[str]]:
    sources: list[VectorSource] = []
    errors: list[str] = []
    seen = set()
    for shp in list_shp_sources(folder):
        key = ("shp", shp.source_path.resolve(), None)
        if key not in seen:
            seen.add(key)
            sources.append(shp)
    for gdb in iter_gdb_paths(folder):
        try:
            gdb_sources = list_gdb_polygon_layers(gdb)
        except Exception as exc:
            errors.append(f"{gdb}: {exc}")
            continue
        for source in gdb_sources:
            key = (source.kind, source.source_path.resolve(), source.layer_name)
            if key not in seen:
                seen.add(key)
                sources.append(source)
    return sources, errors


def merge_source_lists(existing: list[VectorSource], incoming: list[VectorSource]) -> list[VectorSource]:
    merged: list[VectorSource] = []
    seen = set()
    for source in [*existing, *incoming]:
        key = (source.kind, source.source_path.resolve(), source.layer_name)
        if key in seen:
            continue
        seen.add(key)
        merged.append(source)
    return merged


def is_data_field(field) -> bool:
    if field.type in SKIP_FIELD_TYPES:
        return False
    if field.name.lower() in GEOMETRY_FIELD_NAMES:
        return False
    return True


def read_source_spatial_reference(source: VectorSource) -> ProjectionInfo:
    require_arcpy()
    try:
        desc = arcpy.Describe(source_dataset_path(source))
        sr = desc.spatialReference
    except Exception as exc:
        return ProjectionInfo(source, None, f"投影读取失败：{exc}")
    if sr is None or not getattr(sr, "name", None) or sr.name == "Unknown":
        return ProjectionInfo(source, None, "缺少空间参考，无法识别投影")
    return ProjectionInfo(source, sr, describe_spatial_reference(sr))


def describe_spatial_reference(sr: object | None) -> str:
    if sr is None:
        return "未知投影"
    code = getattr(sr, "factoryCode", 0) or 0
    name = getattr(sr, "name", "") or "未命名"
    return f"{name}；EPSG:{code}" if code else f"{name}；无 EPSG/Authority"


def projection_text(sr: object | None) -> str:
    if sr is None:
        return "未知投影"
    lines = [describe_spatial_reference(sr)]
    text = getattr(sr, "exportToString", lambda: "")()
    if text:
        lines.extend(["", text])
    return "\n".join(lines)


def spatial_reference_equal(a: object | None, b: object | None) -> bool:
    if a is None or b is None:
        return False
    a_code = getattr(a, "factoryCode", 0) or 0
    b_code = getattr(b, "factoryCode", 0) or 0
    return bool(a_code and a_code == b_code) or (
        getattr(a, "exportToString", lambda: "")() == getattr(b, "exportToString", lambda: "")()
    )


def analyze_projection_state(input_sources: list[VectorSource]) -> tuple[list[ProjectionInfo], bool]:
    infos = [read_source_spatial_reference(source) for source in input_sources]
    if any(info.spatial_reference is None for info in infos):
        return infos, False
    first = infos[0].spatial_reference
    return infos, all(spatial_reference_equal(first, info.spatial_reference) for info in infos[1:])


def prepare_sources(
    input_sources: list[VectorSource],
    target_spatial_reference: object,
    temp_dir: Path,
    logger: logging.Logger,
) -> list[PreparedSource]:
    require_arcpy()
    prepared: list[PreparedSource] = []
    for index, source in enumerate(input_sources, start=1):
        source_path = source_dataset_path(source)
        info = read_source_spatial_reference(source)
        if info.spatial_reference is None:
            raise RuntimeError(f"输入数据缺少可识别投影，无法重投影：{source_label(source)}")
        if spatial_reference_equal(info.spatial_reference, target_spatial_reference):
            prepared.append(PreparedSource(source_path, source))
            logger.info("投影一致，直接参与分析：%s", source_path_for_log(source))
            continue
        out_path = temp_dir / f"projected_{index:03d}.gdb"
        arcpy.management.CreateFileGDB(str(temp_dir), out_path.name)
        out_fc = str(out_path / "projected")
        logger.info("重投影：%s -> %s", source_path_for_log(source), out_fc)
        arcpy.management.Project(source_path, out_fc, target_spatial_reference)
        prepared.append(PreparedSource(out_fc, source))
    return prepared


class ScrollableCheckList(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.selected_keys: set[str] = set()
        self.source_map: dict[str, VectorSource] = {}
        self.tree = ttk.Treeview(self, columns=("selected", "type", "source"), show="headings", height=10, selectmode="browse")
        self.tree.heading("selected", text="选中")
        self.tree.heading("type", text="类型")
        self.tree.heading("source", text="数据源")
        self.tree.column("selected", width=60, anchor="center", stretch=False)
        self.tree.column("type", width=80, anchor="center", stretch=False)
        self.tree.column("source", width=1500, anchor="w", stretch=False)
        y_scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._click_event)
        self.tree.bind("<Double-1>", self._toggle_event)
        self.tree.bind("<space>", self._toggle_event)

    def set_sources(self, sources: list[VectorSource]) -> None:
        existing_selected = set(self.selected_keys)
        self.selected_keys.clear()
        self.source_map.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for source in sources:
            key = source_path_for_log(source)
            self.source_map[key] = source
            if key in existing_selected:
                self.selected_keys.add(key)
            self.tree.insert("", END, iid=key, values=("✓" if key in self.selected_keys else "", source.kind.upper(), source_label(source)))

    def _toggle_event(self, _event=None) -> str:
        self.toggle_current()
        return "break"

    def _click_event(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        column = self.tree.identify_column(event.x)
        item_id = self.tree.identify_row(event.y)
        if region == "cell" and column == "#1" and item_id:
            self.toggle_key(item_id)

    def toggle_current(self) -> None:
        item_id = self.tree.focus()
        if not item_id:
            selected = self.tree.selection()
            item_id = selected[0] if selected else ""
        if item_id:
            self.toggle_key(item_id)

    def toggle_key(self, key: str) -> None:
        if key in self.selected_keys:
            self.selected_keys.remove(key)
        else:
            self.selected_keys.add(key)
        source = self.source_map[key]
        self.tree.item(key, values=("✓" if key in self.selected_keys else "", source.kind.upper(), source_label(source)))

    def selected_sources(self) -> list[VectorSource]:
        return [self.source_map[key] for key in self.tree.get_children() if key in self.selected_keys]

    def select_all(self, value: bool) -> None:
        self.selected_keys = set(self.source_map.keys()) if value else set()
        for key, source in self.source_map.items():
            self.tree.item(key, values=("✓" if key in self.selected_keys else "", source.kind.upper(), source_label(source)))
