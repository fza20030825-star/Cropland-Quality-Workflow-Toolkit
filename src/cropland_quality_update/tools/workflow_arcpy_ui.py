"""Unified Tkinter UI for the four-step cropland quality workflow."""

from __future__ import annotations

from pathlib import Path
from tkinter import BOTH, END, HORIZONTAL, LEFT, VERTICAL, Canvas, IntVar, StringVar, Text, Tk, filedialog, messagebox, ttk

from cropland_quality_update.tools import area_balance_arcpy_ui as area_tool
from cropland_quality_update.tools import fill_tillage_depth_arcpy_ui as depth_tool
from cropland_quality_update.tools import gb_to_sanpu_arcpy_ui as convert_tool
from cropland_quality_update.tools import membership_arcpy_ui as membership_tool
from cropland_quality_update.tools import recalculate_scores_arcpy_ui as recalc_tool
from cropland_quality_update.tools import update_land_blocks_arcpy_ui as land_tool
from cropland_quality_update.tools import update_scores_arcpy_ui as score_tool


WINDOW_GEOMETRY = "1280x900"
WINDOW_MIN_SIZE = (1180, 760)
SCROLLABLE_WIDGET_CLASSES = {"Treeview", "Text", "Listbox", "Canvas"}
SCROLLBAR_WIDGET_CLASSES = {"Scrollbar", "TScrollbar"}
STEP_LABELS = {
    1: "第一步",
    2: "第二步",
    3: "第三步",
    4: "第四步",
    5: "辅助：国标转三普",
    6: "辅助：补耕层厚度",
    7: "辅助：重算隶属度",
}


class ScrollableStepFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = Canvas(self, borderwidth=0, highlightthickness=0)
        self.content = ttk.Frame(self.canvas)
        self.y_scroll = ttk.Scrollbar(self, orient=VERTICAL, command=self.canvas.yview)
        self.x_scroll = ttk.Scrollbar(self, orient=HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.y_scroll.set, xscrollcommand=self.x_scroll.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.y_scroll.grid(row=0, column=1, sticky="ns")
        self.x_scroll.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._sync_content_width)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _sync_scroll_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_content_width(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _bind_mousewheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Shift-MouseWheel>", self._on_shift_mousewheel)

    def _unbind_mousewheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Shift-MouseWheel>")

    def _event_in_nested_scrollable(self, event) -> bool:
        widget = getattr(event, "widget", None)
        while widget is not None:
            if widget is self.canvas or widget is self.content:
                return False
            widget_class = str(widget.winfo_class())
            if widget_class in SCROLLBAR_WIDGET_CLASSES:
                return True
            if widget_class in SCROLLABLE_WIDGET_CLASSES:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_mousewheel(self, event):
        if self._event_in_nested_scrollable(event):
            return "break"
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _on_shift_mousewheel(self, event):
        if self._event_in_nested_scrollable(event):
            return "break"
        self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)
        self.canvas.xview_moveto(0)


class WorkflowApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Cropland Quality Workflow Toolkit")
        self.root.geometry(WINDOW_GEOMETRY)
        self.root.minsize(*WINDOW_MIN_SIZE)

        self.current_step = IntVar(value=1)
        self.shared_output_gdb_var = StringVar()
        self.step_pages: dict[int, ScrollableStepFrame] = {}
        self.step_apps: dict[int, object] = {}
        self.active_page: ScrollableStepFrame | None = None
        self._syncing_output_gdb = False
        self._syncing_area_selection = False

        self.build_ui()
        self.show_step(1)

    def build_ui(self) -> None:
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        selector = ttk.LabelFrame(self.root, text="流程选择")
        selector.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        selector.columnconfigure(7, weight=1)
        steps = [
            (1, "第一步 计算高标隶属度"),
            (2, "第二步 更新隶属度"),
            (3, "第三步 更新三调图斑"),
            (4, "第四步 面积平差"),
            (5, "辅助 国标转三普"),
            (6, "辅助 补耕层厚度"),
            (7, "辅助 重算隶属度"),
        ]
        for column, (index, label) in enumerate(steps):
            ttk.Radiobutton(
                selector,
                text=label,
                variable=self.current_step,
                value=index,
                command=lambda step=index: self.show_step(step),
            ).grid(row=0, column=column, padx=8, pady=6, sticky="w")
        ttk.Button(selector, text="恢复默认输入", command=self.reset_workflow_inputs).grid(row=0, column=7, padx=12, pady=6, sticky="e")

        gdb_row = ttk.Frame(selector)
        gdb_row.grid(row=1, column=0, columnspan=8, sticky="ew", padx=8, pady=(0, 6))
        gdb_row.columnconfigure(1, weight=1)
        ttk.Label(gdb_row, text="统一输出 GDB").grid(row=0, column=0, sticky="w")
        ttk.Entry(gdb_row, textvariable=self.shared_output_gdb_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(gdb_row, text="选择已有 gdb", command=self.choose_shared_existing_output_gdb).grid(row=0, column=2, padx=3)
        ttk.Button(gdb_row, text="新建 gdb", command=self.choose_shared_output_gdb).grid(row=0, column=3, padx=3)

        self.page_host = ttk.Frame(self.root)
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)

        feedback = ttk.LabelFrame(self.root, text="详细信息")
        feedback.grid(row=2, column=0, sticky="nsew", padx=10, pady=(5, 10))
        feedback.rowconfigure(0, weight=1)
        feedback.columnconfigure(0, weight=1)
        self.status_text = Text(feedback, height=8, wrap="none")
        y_scroll = ttk.Scrollbar(feedback, orient=VERTICAL, command=self.status_text.yview)
        x_scroll = ttk.Scrollbar(feedback, orient=HORIZONTAL, command=self.status_text.xview)
        self.status_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.status_text.grid(row=0, column=0, sticky="nsew", padx=(5, 0), pady=(5, 0))
        y_scroll.grid(row=0, column=1, sticky="ns", pady=(5, 0))
        x_scroll.grid(row=1, column=0, sticky="ew", padx=(5, 0), pady=(0, 5))

        self.create_step_pages()

    def create_step_pages(self) -> None:
        for step in range(1, 8):
            page = ScrollableStepFrame(self.page_host)
            self.step_pages[step] = page

        self.step_apps[1] = membership_tool.MembershipApp(
            self.step_pages[1].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(1, payload),
        )
        self.step_apps[2] = score_tool.ScoreUpdateApp(
            self.step_pages[2].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(2, payload),
        )
        self.step_apps[3] = land_tool.LandBlockUpdateApp(
            self.step_pages[3].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(3, payload),
        )
        self.step_apps[4] = area_tool.AreaBalanceApp(
            self.step_pages[4].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(4, payload),
        )
        self.step_apps[5] = convert_tool.GbToSanpuApp(
            self.step_pages[5].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(5, payload),
        )
        self.step_apps[6] = depth_tool.TillageDepthFillApp(
            self.step_pages[6].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(6, payload),
        )
        self.step_apps[7] = recalc_tool.RecalculateScoresApp(
            self.step_pages[7].content,
            embedded=True,
            shared_status_text=self.status_text,
            on_job_done=lambda payload: self.handle_step_done(7, payload),
        )
        self.setup_output_gdb_sync()
        self.setup_area_selection_sync()

    def setup_output_gdb_sync(self) -> None:
        self.shared_output_gdb_var.trace_add("write", lambda *_args: self.sync_output_gdb(self.shared_output_gdb_var.get()))
        for step, app in self.step_apps.items():
            output_var = getattr(app, "output_gdb_var", None)
            if output_var is not None:
                output_var.trace_add("write", lambda *_args, var=output_var, source_step=step: self.sync_output_gdb(var.get(), source_step))

    def sync_output_gdb(self, value: str, source_step: int | None = None) -> None:
        if self._syncing_output_gdb:
            return
        self._syncing_output_gdb = True
        try:
            if self.shared_output_gdb_var.get() != value:
                self.shared_output_gdb_var.set(value)
            for app in self.step_apps.values():
                output_var = getattr(app, "output_gdb_var", None)
                if output_var is not None and output_var.get() != value:
                    output_var.set(value)
        finally:
            self._syncing_output_gdb = False
        if value.strip().lower().endswith(".gdb"):
            source_text = f"第 {source_step} 步" if source_step else "统一输出栏"
            self.log_status(f"{source_text}已更新输出 GDB，已同步到全部步骤：{value}")

    def setup_area_selection_sync(self) -> None:
        for step, app in self.step_apps.items():
            area_var = getattr(app, "area_var", None)
            if area_var is not None:
                area_var.trace_add("write", lambda *_args, var=area_var, source_step=step: self.sync_area_selection(var.get(), source_step))

    def ensure_area_option(self, app, value: str) -> bool:
        options = list(getattr(app, "area_options", []) or [])
        if value in options:
            return True
        loader = getattr(app, "load_area_options", None)
        if not callable(loader):
            return False
        try:
            options = list(loader())
        except Exception:
            return False
        setattr(app, "area_options", options)
        combo = getattr(app, "area_combo", None)
        if combo is not None:
            combo["values"] = options
        return value in options

    def sync_area_selection(self, value: str, source_step: int | None = None) -> None:
        value = value.strip()
        if self._syncing_area_selection or not value:
            return
        self._syncing_area_selection = True
        synced = 0
        try:
            for app in self.step_apps.values():
                area_var = getattr(app, "area_var", None)
                if area_var is None or not self.ensure_area_option(app, value):
                    continue
                if area_var.get() != value:
                    area_var.set(value)
                for attr in ("last_report", "last_report_key"):
                    if hasattr(app, attr):
                        setattr(app, attr, None)
                synced += 1
        finally:
            self._syncing_area_selection = False
        if synced:
            source_text = STEP_LABELS.get(source_step, "工具") if source_step else "工具"
            self.log_status(f"{source_text}已更新国标二级农业区，已同步到全部相关工具：{value}")

    def choose_shared_output_gdb(self) -> None:
        path = filedialog.asksaveasfilename(title="选择或新建统一输出 FileGDB", defaultextension=".gdb", filetypes=[("File Geodatabase", "*.gdb")])
        if path:
            if not path.lower().endswith(".gdb"):
                path += ".gdb"
            self.shared_output_gdb_var.set(path)

    def choose_shared_existing_output_gdb(self) -> None:
        path = filedialog.askdirectory(title="选择统一输出 .gdb 文件夹")
        if not path:
            return
        gdb_path = membership_tool.find_nearest_gdb_path(Path(path))
        if gdb_path is None or not membership_tool.is_gdb_path(gdb_path):
            messagebox.showwarning("提示", "请选择一个已有的 .gdb 文件夹。")
            return
        self.shared_output_gdb_var.set(str(gdb_path))

    def show_step(self, step: int) -> None:
        if self.active_page is not None:
            self.active_page.grid_remove()
        page = self.step_pages[step]
        page.grid(row=0, column=0, sticky="nsew")
        page.scroll_to_top()
        self.active_page = page
        self.current_step.set(step)

    def handle_step_done(self, step: int, payload: dict) -> None:
        output_path = payload.get("output_path")
        if not output_path:
            return
        try:
            if step == 1:
                self.fill_score_result_input(output_path)
                self.show_step(2)
            elif step == 2:
                self.fill_land_result_input(output_path)
                self.show_step(3)
            elif step == 3:
                self.fill_area_balance_input(output_path)
                self.show_step(4)
            elif step == 5:
                self.fill_score_result_input(output_path, "国标转三普输出")
                self.log_status(f"国标转三普输出已填入第二步输入，当前仍停留在辅助工具：{output_path}")
            elif step == 6:
                self.log_status(f"补耕层厚度输出完成，可作为国标转三普辅助工具的输入：{output_path}")
            elif step == 7:
                self.fill_area_balance_input(output_path)
                self.log_status(f"重算隶属度输出已填入第四步输入，当前仍停留在辅助工具：{output_path}")
        except Exception as exc:
            self.log_status(f"自动填写下一步输入失败：{exc}")

    def source_from_output_path(self, output_path: str):
        path = Path(output_path)
        if path.suffix.lower() == ".shp":
            return membership_tool.make_vector_source("shp", path)
        for candidate in [path, *path.parents]:
            if candidate.suffix.lower() == ".gdb":
                relative = path.relative_to(candidate)
                layer_name = str(relative)
                if not layer_name or layer_name == ".":
                    raise ValueError(f"GDB 输出路径缺少要素类名：{output_path}")
                return membership_tool.make_vector_source("gdb", candidate, layer_name)
        raise ValueError(f"无法识别输出数据类型：{output_path}")

    def fill_score_result_input(self, output_path: str, source_name: str = "第一步输出") -> None:
        app = self.step_apps[2]
        source = self.source_from_output_path(output_path)
        self.set_single_source(
            app,
            source,
            path_var_name="result_path_var",
            layer_var_name="result_layer_var",
            source_attr="result_source",
            sources_attr="result_gdb_sources",
            combo_attr="result_layer_combo",
            reset_attrs=("last_report", "last_report_key"),
        )
        app.update_target_projection()
        app.log_status(f"已自动填写{source_name}到第二步输入：{source.display_name}")

    def fill_land_result_input(self, output_path: str) -> None:
        app = self.step_apps[3]
        source = self.source_from_output_path(output_path)
        self.set_single_source(
            app,
            source,
            path_var_name="result_path_var",
            layer_var_name="result_layer_var",
            source_attr="result_source",
            sources_attr="result_gdb_sources",
            combo_attr="result_layer_combo",
            reset_attrs=("last_report", "last_report_key"),
        )
        app.update_target_projection()
        app.log_status(f"已自动填写第二步输出到第三步输入：{source.display_name}")

    def fill_area_balance_input(self, output_path: str) -> None:
        app = self.step_apps[4]
        source = self.source_from_output_path(output_path)
        self.set_single_source(
            app,
            source,
            path_var_name="source_path_var",
            layer_var_name="source_layer_var",
            source_attr="source",
            sources_attr="gdb_sources",
            combo_attr="layer_combo",
            reset_attrs=("last_report", "last_report_key"),
        )
        app.log_status(f"已自动填写第三步输出到第四步输入：{source.display_name}")

    @staticmethod
    def set_single_source(
        app,
        source,
        *,
        path_var_name: str,
        layer_var_name: str,
        source_attr: str,
        sources_attr: str,
        combo_attr: str,
        reset_attrs: tuple[str, ...],
    ) -> None:
        getattr(app, path_var_name).set(str(source.source_path))
        getattr(app, layer_var_name).set(source.layer_name or "")
        setattr(app, source_attr, source)
        combo = getattr(app, combo_attr, None)
        if source.kind == "gdb":
            setattr(app, sources_attr, [source])
            if combo is not None:
                combo["values"] = [source.layer_name]
        else:
            setattr(app, sources_attr, [])
            if combo is not None:
                combo["values"] = []
        for attr in reset_attrs:
            if hasattr(app, attr):
                setattr(app, attr, None)

    def log_status(self, message: str) -> None:
        self.status_text.insert(END, f"{message}\n")
        self.status_text.see(END)

    def reset_workflow_inputs(self) -> None:
        self._syncing_area_selection = True
        try:
            for app in self.step_apps.values():
                reset = getattr(app, "reset_inputs", None)
                if callable(reset):
                    reset()
        finally:
            self._syncing_area_selection = False
        self.shared_output_gdb_var.set("")
        self.show_step(1)
        self.log_status("已恢复各步骤输入和参数的启动默认值；详细信息记录保留。")


def main() -> int:
    for require in (
        membership_tool.require_runtime,
        score_tool.require_runtime,
        land_tool.require_runtime,
        area_tool.require_runtime,
        convert_tool.require_runtime,
        depth_tool.require_runtime,
        recalc_tool.require_runtime,
    ):
        try:
            require()
        except Exception as exc:
            root = Tk()
            root.withdraw()
            from tkinter import messagebox

            messagebox.showerror("缺少运行环境", str(exc))
            root.destroy()
            return 1
    root = Tk()
    WorkflowApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
