# DWG Reader Skill

读取本地 AutoCAD DWG / DXF 图纸，将图层、布局、块内容、文字、标注和支持的几何数据提取为 Markdown 或 JSON，供支持 `SKILL.md` 的 AI 助手使用，也可以独立运行。

## 能力

- 读取模型空间与图纸布局，展开嵌套块、块属性和常量属性。
- 支持按布局、图层、实体类型和近似空间范围筛选。
- 完整 JSON 不受摘要数量限制，保留全部已支持的文字、顶点和样条数组。
- 输出路径保护、转换超时与错误检查、临时文件清理、明确的部分提取警告。
- JSON schema v2：坐标使用数值数组；测量失败返回 `null`，不会伪装成零。
- 可选 PNG 预览，以及通过 `--blocks` 导出块定义的实体统计和文字。

## 安装

需要 Python 3。当前版本使用 Python 3.12 和 ezdxf 1.4.4 验证。

```bash
python -m pip install -r requirements.txt
```

PNG 预览及完整测试还需要可选的绘图依赖：

```bash
python -m pip install -r requirements-render.txt
```

作为 skill 使用时，把本仓库放到工具支持的技能目录，并保持以下结构：

```text
dwg-reader/
  SKILL.md
  scripts/
    dwg_to_data.py
    test_dwg_to_data.py
```

DXF 不需要转换器。读取 DWG 还需要安装 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) 或 LibreDWG 的 `dwg2dxf`。转换器不包含在本仓库中。

## 使用

```bash
# 查看文字摘要
python scripts/dwg_to_data.py drawing.dxf --text-only

# 同时导出 JSON 和 Markdown
python scripts/dwg_to_data.py drawing.dxf --json drawing.json --out report.md

# 按布局和图层筛选
python scripts/dwg_to_data.py drawing.dxf --layout Model --layer WALLS

# 空间筛选（包围盒近似，不是精确几何相交）
python scripts/dwg_to_data.py drawing.dxf --bbox=-10,-10,100,80

# 显式指定 DWG 转换器
python scripts/dwg_to_data.py drawing.dwg --converter "/path/to/ODAFileConverter"

# PNG 预览与块定义导出（需要绘图依赖）
python scripts/dwg_to_data.py drawing.dxf --png preview.png --blocks --json drawing.json
```

Windows 可使用 `py -3` 或已安装环境的 Python 完整路径；macOS / Linux 可使用 `python3`。更多选项见 [SKILL.md](SKILL.md) 和 `--help`。

## 验证

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
```

30 项测试覆盖中文文字、布局、嵌套块、常量属性、标注、筛选、完整导出、原图保护、转换错误处理、PNG 预览和块定义导出。测试使用生成的样例；检测到 ODA 转换器时还会执行 DXF → DWG → DXF 往返测试，没有合适转换器时跳过该项。代表性生产图纸仍需单独验证。

## 限制

PNG 只是基于 ezdxf/matplotlib 的辅助预览，不能替代 CAD 软件的正式出图。空间查询按 WCS XY 包围盒近似筛选；字体、曲线、非均匀缩放和不支持的对象可能影响结果。标注的几何测量值不等于 CAD 中最终显示的文字，需结合类型、单位、比例和覆盖值解释。

不加载外部参照，不提取光栅像素、实体模型和代理对象的完整几何。请检查输出中的 `warnings`、`geometry_status` 和 `extraction_error`。`--raw` 仅为不可靠的 ASCII 二进制诊断，不能可靠读取中文或压缩 DWG 内容。

仓库不包含用户图纸、访问凭据、转换器或本机测试环境。
