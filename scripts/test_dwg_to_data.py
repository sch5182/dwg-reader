"""Regression tests; run with the same Python environment as dwg_to_data.py."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ezdxf
from ezdxf.entities import Dimension
from ezdxf.math import Vec2
import dwg_to_data as reader


class ReaderTests(unittest.TestCase):
    def args(self, *extra):
        return reader.parser().parse_args(["unused.dxf", *extra])

    def extract(self, doc, *extra):
        return reader.extract(doc, "sample.dxf", self.args(*extra), reader.Warnings())

    def test_nested_block_layout_text_and_transform(self):
        doc = ezdxf.new()
        doc.layers.new("WALLS")
        inner = doc.blocks.new("INNER")
        inner.add_text("中文房间", dxfattribs={"insert": (1, 0), "height": 1})
        outer = doc.blocks.new("OUTER")
        outer.add_blockref("INNER", (10, 0))
        doc.modelspace().add_blockref("OUTER", (100, 200), dxfattribs={"rotation": 90, "layer": "WALLS"})
        doc.layout().add_text("布局说明")
        result = self.extract(doc)
        text = next(x for x in result["text_content"] if x["text"] == "中文房间")
        self.assertAlmostEqual(text["at"][0], 100)
        self.assertAlmostEqual(text["at"][1], 211)
        self.assertEqual(text["layer"], "WALLS")
        self.assertEqual(len(text["block_path"]), 2)
        self.assertIn("布局说明", [x["text"] for x in result["text_content"]])
        only_model = self.extract(doc, "--layout", "model", "--types", "TEXT", "--layer", "walls")
        self.assertEqual([x["text"] for x in only_model["text_content"]], ["中文房间"])

    def test_attributes_are_filtered_as_entities(self):
        doc = ezdxf.new()
        block = doc.blocks.new("B")
        block.add_attdef("TAG", (0, 0), text="default")
        insert = doc.modelspace().add_blockref("B", (10, 0))
        insert.add_auto_attribs({"TAG": "instance"})
        result = self.extract(doc, "--types", "ATTRIB")
        self.assertEqual([t["text"] for t in result["text_content"]], ["instance"])

    def test_constant_attdef_and_minsert(self):
        doc = ezdxf.new()
        block = doc.blocks.new("B")
        block.add_attdef("CONST", (0, 0), text="fixed", dxfattribs={"flags": 2})
        insert = doc.modelspace().add_blockref("B", (10, 0))
        insert.dxf.column_count = 2
        insert.dxf.column_spacing = 20
        result = self.extract(doc)
        texts = [r for r in result["text_content"] if r["text"] == "fixed"]
        self.assertEqual(len(texts), 2)
        self.assertEqual(sorted(t["at"][0] for t in texts), [10, 30])

    def test_no_json_truncation_or_text_only_data_loss(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        for i in range(405):
            msp.add_text(f"label-{i}")
        msp.add_lwpolyline([(i, i % 3, 0, 0, .5) for i in range(75)])
        result = self.extract(doc, "--text-only", "--max-texts", "1", "--max-entities", "0")
        self.assertEqual(len(result["text_content"]), 405)
        poly = next(r for r in result["entities"] if r["type"] == "LWPOLYLINE")
        self.assertEqual(len(poly["vertices"]), 75)
        self.assertEqual(poly["widths_and_bulges"][0][-1], .5)
        self.assertIsInstance(poly["vertices"][0][0], float)
        self.assertFalse(result["truncated"])
        summary = reader.markdown(result, self.args("--text-only", "--max-texts", "1"))
        self.assertIn("1 of 405 shown", summary)
        self.assertIn("404 text items omitted", summary)
        full = reader.markdown(result, self.args("--full"))
        self.assertIn("405 of 405 shown", full)
        self.assertNotIn("omitted", full)

    def test_units_and_dimension_error_never_zero(self):
        doc = ezdxf.new(units=14)
        dim = doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0)).dimension
        result = self.extract(doc)
        self.assertEqual(result["units"].lower(), "decimeters")
        rec = next(r for r in result["entities"] if r["type"] == "DIMENSION")
        self.assertEqual(rec["measurement"], 10)
        with patch.object(Dimension, "get_measurement", side_effect=ValueError("broken geometry")):
            details = reader.entity_details(dim, reader.Warnings())
        self.assertIsNone(details["measurement"])
        self.assertEqual(details["measurement_status"], "unavailable")

    def test_angular_dimension_uses_degrees(self):
        doc = ezdxf.new()
        dim = doc.modelspace().add_angular_dim_3p(base=(4, 4), center=(0, 0), p1=(5, 0), p2=(0, 5)).dimension
        details = reader.entity_details(dim, reader.Warnings())
        self.assertAlmostEqual(details["measurement"], 90)
        self.assertEqual(details["measurement_unit"], "degrees")

    def test_dimension_override_and_scale_preserved(self):
        doc = ezdxf.new()
        builder = doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0), override={"dimlfac": 100})
        builder.commit()
        dim = builder.dimension
        dim.dxf.text = "NOMINAL <>"
        details = reader.entity_details(dim, reader.Warnings())
        self.assertEqual(details["measurement"], 10)
        self.assertEqual(details["dimlfac"], 100)
        self.assertEqual(details["text"], "NOMINAL <>")
        self.assertFalse(details["text_is_rendered_label"])

    def test_block_spatial_query_away_from_insert(self):
        doc = ezdxf.new()
        block = doc.blocks.new("B")
        block.add_line((100, 0), (110, 0))
        doc.modelspace().add_blockref("B", (1000, 0))
        result = self.extract(doc, "--bbox", "1105,-1,1106,1")
        self.assertIn("LINE", [r["type"] for r in result["entities"]])
        inserts = self.extract(doc, "--types", "INSERT", "--near", "1105,0,1")
        self.assertEqual(len(inserts["entities"]), 1)

    def test_arc_bbox_not_entire_circle(self):
        doc = ezdxf.new()
        doc.modelspace().add_arc((0, 0), 10, 0, 90)
        result = self.extract(doc, "--bbox=-11,-11,-9,-9")
        self.assertEqual(result["entities"], [])

    def test_cycle_terminates_with_warning(self):
        doc = ezdxf.new()
        block = doc.blocks.new("LOOP")
        block.add_blockref("LOOP", (0, 0))
        doc.modelspace().add_blockref("LOOP", (0, 0))
        result = self.extract(doc)
        self.assertTrue(any("Cyclic" in w["message"] for w in result["warnings"]))

    def test_invalid_parameters_rejected(self):
        for extra in (("--near", "1,2,-1"), ("--bbox", "1,2"), ("--near", "nan,1,1"), ("--max-texts", "-1")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.args(*extra)

    def test_unknown_layout_rejected(self):
        with self.assertRaises(reader.ReaderError):
            self.extract(ezdxf.new(), "--layout", "not-present")

    def test_input_aliases_protected(self):
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "test.dxf"
            src.write_bytes(b"DO NOT CHANGE")
            for output in (src, src.parent / "." / src.name):
                with self.assertRaises(reader.ReaderError):
                    reader.protect_output(src, (output,))
            alias = Path(work) / "hardlink.json"
            os.link(src, alias)
            with self.assertRaises(reader.ReaderError):
                reader.protect_output(src, (alias,))
            self.assertEqual(src.read_bytes(), b"DO NOT CHANGE")

    def test_cli_protects_input_before_parsing(self):
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "test.dwg"
            src.write_bytes(b"DO NOT CHANGE")
            with contextlib.redirect_stderr(io.StringIO()), patch.object(reader, "find_converter") as find:
                self.assertEqual(reader.main([str(src), "--out", str(src)]), 1)
                find.assert_not_called()
            self.assertEqual(src.read_bytes(), b"DO NOT CHANGE")

    def test_real_dxf_cli_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "中文 sample.dxf"
            doc = ezdxf.new()
            doc.modelspace().add_text("中文说明")
            doc.saveas(src)
            before = src.read_bytes()
            output, report = Path(work) / "data.json", Path(work) / "report.md"
            with contextlib.redirect_stderr(io.StringIO()):
                code = reader.main([str(src), "--json", str(output), "--out", str(report)])
            self.assertEqual(code, 0)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["text_content"][0]["text"], "中文说明")
            self.assertIn("中文说明", report.read_text(encoding="utf-8"))
            self.assertEqual(src.read_bytes(), before)

    def test_converter_failure_is_checked(self):
        with patch.object(reader.subprocess, "run", return_value=subprocess.CompletedProcess([], 7, b"", b"bad CAD")):
            with self.assertRaises(reader.ReaderError) as error:
                reader.run_converter(["fake"], ".")
        self.assertEqual(error.exception.code, 4)

    def test_converter_temp_is_cleaned_after_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as work:
                src = Path(work) / "sample.dwg"
                src.write_bytes(b"fake drawing")
                locations = []

                def converter(source, selected, temp):
                    locations.append(Path(temp))
                    if fail:
                        raise reader.ReaderError("intentional failure", 4)
                    out = Path(temp) / "sample.dxf"
                    ezdxf.new().saveas(out)
                    return out

                with patch.object(reader, "find_converter", return_value=("oda", "fake")), patch.object(reader, "convert_dwg", side_effect=converter), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = reader.main([str(src)])
                self.assertEqual(code, 4 if fail else 0)
                self.assertTrue(locations)
                self.assertTrue(all(not p.exists() for p in locations))

    def test_recovery_import_and_auditor_report(self):
        from ezdxf import recover
        from ezdxf.audit import Auditor
        doc = ezdxf.new()
        warnings = reader.Warnings()
        with patch.object(ezdxf, "readfile", side_effect=ezdxf.DXFStructureError("bad structure")), patch.object(recover, "readfile", return_value=(doc, Auditor(doc))):
            self.assertIs(reader.load_doc("fake.dxf", warnings), doc)
        self.assertTrue(any("recovery" in w["message"].lower() for w in warnings.as_list()))

    def test_raw_honors_outputs_and_marks_unreliable(self):
        with tempfile.TemporaryDirectory() as work:
            src, out = Path(work) / "test.dwg", Path(work) / "raw.json"
            src.write_bytes(b"AC1032 SomeAsciiText")
            with patch.object(reader, "find_converter", return_value=None), contextlib.redirect_stderr(io.StringIO()):
                code = reader.main([str(src), "--raw", "--json", str(out)])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(out.read_text(encoding="utf-8"))["reliable"])

    def test_missing_explicit_converter_is_not_ignored(self):
        with self.assertRaises(reader.ReaderError):
            reader.find_converter("nonexistent-converter-123456.exe")

    def test_other_cad_output_is_rejected(self):
        with self.assertRaises(reader.ReaderError):
            reader.protect_output("source.dxf", ("other.dwg",))

    def test_sibling_insert_bounds_are_distinct(self):
        doc = ezdxf.new()
        child = doc.blocks.new("CHILD")
        child.add_line((0, 0), (5, 0))
        parent = doc.blocks.new("PARENT")
        parent.add_blockref("CHILD", (0, 0))
        parent.add_blockref("CHILD", (100, 0))
        doc.modelspace().add_blockref("PARENT", (0, 0))
        result = self.extract(doc, "--types", "INSERT", "--near", "102,0,1")
        children = [r for r in result["entities"] if r["block"] == "CHILD"]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["at"][0], 100)

    def test_measurement_failure_preserves_override(self):
        doc = ezdxf.new()
        dim = doc.modelspace().add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0)).dimension
        dim.dxf.text = "现场复核"
        with patch.object(Dimension, "get_measurement", side_effect=ValueError("broken")):
            details = reader.entity_details(dim, reader.Warnings())
        self.assertIsNone(details["measurement"])
        self.assertEqual(details["text"], "现场复核")

    def test_external_reference_is_reported(self):
        doc = ezdxf.new()
        block = doc.blocks.new("EXTERNAL", dxfattribs={"flags": 4, "xref_path": "missing.dwg"})
        doc.modelspace().add_blockref(block.name, (0, 0))
        result = self.extract(doc)
        self.assertTrue(any("External reference" in w["message"] for w in result["warnings"]))

    def test_multileader_text(self):
        doc = ezdxf.new()
        builder = doc.modelspace().add_multileader_mtext()
        builder.set_content("多重引线")
        builder.build(insert=Vec2(0, 0))
        result = self.extract(doc)
        self.assertIn("多重引线", [r["text"] for r in result["text_content"]])

    def test_render_png_writes_file(self):
        doc = ezdxf.new()
        doc.modelspace().add_line((0, 0), (10, 0))
        doc.modelspace().add_text("渲染预览")
        with tempfile.TemporaryDirectory() as work:
            out = Path(work) / "preview.png"
            reader.render_layout_png(doc, doc.modelspace(), out, reader.Warnings())
            self.assertGreater(out.stat().st_size, 100)

    def test_png_requires_layout_and_reports_missing_dependency(self):
        doc = ezdxf.new()
        missing = {"matplotlib": None, "ezdxf.addons.drawing": None, "ezdxf.addons.drawing.matplotlib": None}
        with self.assertRaises(reader.ReaderError) as error, patch.dict(sys.modules, missing):
            reader.render_layout_png(doc, doc.modelspace(), Path("unused.png"), reader.Warnings())
        self.assertEqual(error.exception.code, 2)

    def test_block_definitions_contents_and_flag(self):
        doc = ezdxf.new()
        inner = doc.blocks.new("INNER")
        inner.add_text("内层文字")
        outer = doc.blocks.new("OUTER")
        outer.add_blockref("INNER", (0, 0))
        outer.add_line((0, 0), (1, 1))
        self.assertNotIn("block_definitions", self.extract(doc))
        defs = {d["name"]: d for d in reader.block_definitions(doc, reader.Warnings())}
        self.assertEqual(defs["OUTER"]["entity_counts"], {"INSERT": 1, "LINE": 1})
        self.assertEqual([t["text"] for t in defs["INNER"]["text_content"]], ["内层文字"])
        self.assertEqual([t["text"] for t in defs["OUTER"]["text_content"]], ["内层文字"])

    def test_cli_png_and_blocks_flags(self):
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "sample.dxf"
            doc = ezdxf.new()
            doc.blocks.new("CHAIR").add_text("座椅")
            doc.modelspace().add_blockref("CHAIR", (5, 5))
            doc.saveas(src)
            png, json_path = Path(work) / "preview.png", Path(work) / "data.json"
            with contextlib.redirect_stderr(io.StringIO()):
                code = reader.main([str(src), "--png", str(png), "--blocks", "--json", str(json_path)])
            self.assertEqual(code, 0)
            self.assertGreater(png.stat().st_size, 100)
            defs = {d["name"]: d for d in json.loads(json_path.read_text(encoding="utf-8"))["block_definitions"]}
            self.assertIn("座椅", [t["text"] for t in defs["CHAIR"]["text_content"]])

    def test_real_dwg_roundtrip_when_converter_available(self):
        converter = reader.find_converter()
        if converter is None:
            self.skipTest("no DWG converter installed")
        kind, exe = converter
        if kind != "oda":
            self.skipTest("LibreDWG dwg2dxf cannot write DWG files")
        doc = ezdxf.new()
        doc.layers.new("WALLS")
        doc.modelspace().add_line((0, 0), (10, 0))
        doc.modelspace().add_text("往返测试")
        with tempfile.TemporaryDirectory() as work:
            doc.saveas(Path(work) / "rt.dxf")
            outdir = Path(work) / "dwg_out"
            outdir.mkdir()
            reader.run_converter([str(exe), str(work), str(outdir), "ACAD2018", "DWG", "0", "1", "*.dxf"], work)
            dwg = outdir / "rt.dwg"
            self.assertTrue(dwg.is_file())
            with tempfile.TemporaryDirectory() as read_work:
                converted = reader.convert_dwg(dwg, converter, read_work)
                result = reader.extract(reader.load_doc(converted, reader.Warnings()), dwg, self.args(), reader.Warnings())
            self.assertIn("往返测试", [x["text"] for x in result["text_content"]])
            self.assertIn("WALLS", [layer["name"] for layer in result["layers"]])


if __name__ == "__main__":
    unittest.main()
