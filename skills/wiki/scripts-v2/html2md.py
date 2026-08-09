#!/usr/bin/env python3
"""HTML → Wiki Markdown 高保真转换工具。

将 CC Pages 的富 HTML 页面转换为 Quartz 兼容的 Markdown，
保留 SVG 内联图、转换 note→callout、specs→table 等。

用法:
  python3 html2md.py <html_file> [--slug <slug>] [--dry-run]

示例:
  python3 html2md.py ~/gcs-mount/cc-pages/pages/almodel-architecture-guide-20260308.html
"""

import argparse
import base64
import os
import re
import sys
from pathlib import Path
from html.parser import HTMLParser

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import WIKI_CONTENT, find_page_by_slug


class Html2MdConverter:
    """将 CC Pages HTML 转换为高保真 Markdown。"""

    def __init__(self, html: str, source_url: str = None):
        self.html = html
        self.source_url = source_url

    @staticmethod
    def _extract_block(s: str, open_tag: str) -> list[tuple[int, int, str]]:
        """提取指定 open_tag 的完整块（处理嵌套 div）。
        返回 [(start, end, inner_content), ...]"""
        results = []
        search_from = 0
        while True:
            start = s.find(open_tag, search_from)
            if start == -1:
                break
            # 找到 open_tag 之后的内容起点
            inner_start = start + len(open_tag)
            depth = 1
            pos = inner_start
            while depth > 0 and pos < len(s):
                next_open = s.find('<div', pos)
                next_close = s.find('</div>', pos)
                if next_close == -1:
                    break
                if next_open != -1 and next_open < next_close:
                    depth += 1
                    pos = next_open + 4
                else:
                    depth -= 1
                    if depth == 0:
                        inner = s[inner_start:next_close]
                        end = next_close + 6  # len('</div>')
                        results.append((start, end, inner))
                    pos = next_close + 6
            search_from = pos if depth == 0 else len(s)
        return results

    def convert(self) -> str:
        """执行转换，返回 Markdown body（不含 frontmatter）。"""
        s = self.html

        # 提取 <body> 内容
        body_match = re.search(r'<body[^>]*>(.*?)</body>', s, re.DOTALL)
        if body_match:
            s = body_match.group(1)

        # 去掉 container div
        s = re.sub(r'<div class="container">(.*)</div>\s*$', r'\1', s, flags=re.DOTALL)

        # 1. 保护 SVG — 提取所有 SVG 并用占位符替换
        svgs = []
        def save_svg(m):
            idx = len(svgs)
            # 适配亮色主题：替换暗色背景色
            svg = m.group(0)
            svg = self._adapt_svg_colors(svg)
            svgs.append(svg)
            return f'\n\n<!--SVG_PLACEHOLDER_{idx}-->\n\n'

        s = re.sub(r'<svg[^>]*>.*?</svg>', save_svg, s, flags=re.DOTALL)

        # 2. 转换 spec cards → table
        s = self._convert_specs(s)

        # 3. 转换 arch-box (key-value rows) → table
        s = self._convert_arch_box(s)

        # 4. 转换 timeline-phase → structured callout
        s = self._convert_timeline(s)

        # 5a. 转换 layer-diagram → table
        s = self._convert_layer_diagram(s)
        # 5b. 转换 tree div → code block (目录树等预格式化内容)
        s = self._convert_tree(s)

        # 6. 转换 note/warning → callout
        s = self._convert_notes(s)

        # 6. 转换 compare → table
        s = self._convert_compare(s)

        # 7a. 转换 grid 里的 stat cards → 合并 callout
        s = self._convert_stat_grid(s)
        # 7b. 转换剩余 card → callout
        s = self._convert_cards(s)

        # 8a. 转换 bar-chart → table
        s = self._convert_bar_chart(s)
        # 8b. 转换 param-bar → 描述文本
        s = self._convert_param_bar(s)

        # 9. 转换 badge spans → bold
        s = re.sub(r'<span class="badge[^"]*">([^<]*)</span>', r'**\1**', s)

        # 10. 转换 tag spans → bold
        s = re.sub(r'<span class="tag[^"]*">([^<]*)</span>', r'**\1**', s)
        s = re.sub(r'<span class="fuse-tag[^"]*">([^<]*)</span>', r'`\1`', s)
        s = re.sub(r'<span class="path">([^<]*)</span>', r'**\1**', s)
        s = re.sub(r'<span class="comment">([^<]*)</span>', r'\1', s)

        # 11. 转换 label div → bold
        s = re.sub(r'<div class="label">(.*?)</div>', r'\n**\1**\n', s, flags=re.DOTALL)

        # 11. 转换 svg-caption
        s = re.sub(r'<p class="svg-caption">(.*?)</p>', r'*\1*', s, flags=re.DOTALL)

        # 12. 转换 explain-term → wikilink
        s = re.sub(
            r'<span class="explain-term" data-explain="([^"]*)">(.*?)</span>',
            r'[[\1|\2]]', s
        )

        # 13. 移除纯装饰性 div（size-bar 等），在标准 HTML 转换前
        s = re.sub(r'<div class="size-bar[^"]*"[^>]*>\s*</div>', '', s)
        s = re.sub(r'<div class="permission-warn">(.*?)</div>', r'\n> [!warning]\n> \1\n', s, flags=re.DOTALL)

        # 13b. 检测 <p> 内的 ASCII art（box-drawing 字符）→ 转为 <pre><code>
        s = self._convert_ascii_art_paragraphs(s)

        # 14. 标准 HTML → Markdown
        s = self._convert_standard_html(s)

        # 14. 还原 SVG — 转为 base64 data URI <img> 嵌入
        # （enableInHtmlEmbed 会拆解 inline SVG 的 <text> 标签，data URI 避免此问题）
        for i, svg in enumerate(svgs):
            img_tag = self._svg_to_data_uri_img(svg)
            s = s.replace(f'<!--SVG_PLACEHOLDER_{i}-->', img_tag)

        # 15. 清理
        s = self._cleanup(s)

        return s

    @staticmethod
    def _svg_to_data_uri_img(svg: str) -> str:
        """将 SVG 转为 base64 data URI <img> 标签。
        避免 Quartz enableInHtmlEmbed 解析 SVG 内部 <text> 元素。"""
        # 提取第一个 <text> 作为 alt（在实体替换前提取，保留可读性）
        title_match = re.search(r'<text[^>]*>([^<]+)</text>', svg)
        alt = title_match.group(1).strip() if title_match else 'SVG Diagram'
        # 替换 HTML entities → Unicode（XML 只认 &lt; &gt; &amp; &quot; &apos;）
        # 先保护 XML 合法实体，html.unescape 全部转换，再恢复
        import html as html_mod
        svg = svg.replace('&amp;', '\x00AMP\x00')
        svg = svg.replace('&lt;', '\x00LT\x00')
        svg = svg.replace('&gt;', '\x00GT\x00')
        svg = svg.replace('&quot;', '\x00QUOT\x00')
        svg = svg.replace('&apos;', '\x00APOS\x00')
        svg = html_mod.unescape(svg)
        svg = svg.replace('\x00AMP\x00', '&amp;')
        svg = svg.replace('\x00LT\x00', '&lt;')
        svg = svg.replace('\x00GT\x00', '&gt;')
        svg = svg.replace('\x00QUOT\x00', '&quot;')
        svg = svg.replace('\x00APOS\x00', '&apos;')
        # base64 编码
        b64 = base64.b64encode(svg.encode('utf-8')).decode('ascii')
        return f'<img src="data:image/svg+xml;base64,{b64}" alt="{alt}" style="width:100%;max-width:900px;" />'

    def _adapt_svg_colors(self, svg: str) -> str:
        """适配 SVG 颜色到亮色/暗色主题兼容。"""
        # 暗色背景文字 → 使用 currentColor 或深色
        svg = svg.replace('fill="#e6edf3"', 'fill="currentColor"')
        svg = svg.replace('fill="#8b949e"', 'fill="var(--gray, #64748b)"')
        # 暗色背景 → 透明
        svg = svg.replace('fill="#0d1117"', 'fill="var(--light, #f8fafc)"')
        svg = svg.replace('fill="#161b22"', 'fill="var(--highlight, rgba(0,0,0,0.04))"')
        # 暗色边框 → 主题边框色
        svg = svg.replace('stroke="#30363d"', 'stroke="var(--lightgray, #e2e8f0)"')
        svg = svg.replace('fill="#30363d"', 'fill="var(--lightgray, #e2e8f0)"')
        return svg

    def _convert_specs(self, s: str) -> str:
        """转换 <div class="specs"> 卡片网格 → Markdown table。"""
        for start, end, inner in reversed(self._extract_block(s, '<div class="specs">')):
            specs = re.findall(
                r'<div class="spec">.*?<div class="spec-label">(.*?)</div>.*?'
                r'<div class="spec-value">(.*?)</div>.*?'
                r'<div class="spec-detail">(.*?)</div>.*?</div>',
                inner, re.DOTALL
            )
            if specs:
                rows = ['| 参数 | 值 | 说明 |', '|------|-----|------|']
                for label, value, detail in specs:
                    rows.append(f'| {label.strip()} | **{value.strip()}** | {detail.strip()} |')
                s = s[:start] + '\n'.join(rows) + '\n' + s[end:]
        return s

    def _convert_arch_box(self, s: str) -> str:
        """转换 <div class="arch-box"> (key-value rows) → Markdown table。
        原始结构: <div class="arch-row"><span class="arch-label">Key</span><span class="arch-value">Val</span></div>
        """
        for start, end, inner in reversed(self._extract_block(s, '<div class="arch-box">')):
            rows_data = re.findall(
                r'<span class="arch-label">(.*?)</span>\s*<span class="arch-value">(.*?)</span>',
                inner, re.DOTALL
            )
            if rows_data:
                rows = ['| 参数 | 值 |', '|------|-----|']
                for label, value in rows_data:
                    label = self._inline_html(label).strip()
                    value = self._inline_html(value).strip()
                    rows.append(f'| {label} | {value} |')
                s = s[:start] + '\n' + '\n'.join(rows) + '\n' + s[end:]
            else:
                # No arch-row found, just strip the div
                s = s[:start] + self._inline_html(inner) + s[end:]
        return s

    def _convert_timeline(self, s: str) -> str:
        """转换 <div class="timeline-phase"> → 结构化段落。
        原始结构: phase-date + phase-desc + phase-hw
        """
        # Match all timeline-phase variants (highlight, live, etc.)
        pattern = r'<div class="timeline-phase[^"]*"[^>]*>'
        for start, end, inner in reversed(self._extract_block(s, pattern)):
            # 重新搜索确切的 open tag
            pass
        # Use regex to find and replace each timeline-phase block
        def replace_phase(m):
            block = m.group(0)
            date_m = re.search(r'<div class="phase-date">(.*?)</div>', block, re.DOTALL)
            desc_m = re.search(r'<div class="phase-desc">(.*?)</div>', block, re.DOTALL)
            hw_m = re.search(r'<div class="phase-hw">(.*?)</div>', block, re.DOTALL)

            parts = []
            if date_m:
                parts.append(f'**{self._inline_html(date_m.group(1)).strip()}**\n')
            if desc_m:
                desc = self._inline_html(desc_m.group(1)).strip()
                parts.append(desc + '\n')
            if hw_m:
                hw = self._inline_html(hw_m.group(1)).strip()
                parts.append(f'*{hw}*\n')

            return '\n' + '\n'.join(parts) + '\n'

        # Use _extract_block for proper nested div handling
        search_tags = [
            '<div class="timeline-phase live"',
            '<div class="timeline-phase highlight"',
            '<div class="timeline-phase"',
        ]
        for tag_prefix in search_tags:
            # Find all opening tags matching this prefix
            search_from = 0
            while True:
                idx = s.find(tag_prefix, search_from)
                if idx == -1:
                    break
                # Find the end of the opening tag
                tag_end = s.find('>', idx)
                if tag_end == -1:
                    break
                open_tag = s[idx:tag_end + 1]
                blocks = self._extract_block(s[idx:], open_tag)
                if blocks:
                    bstart, bend, inner = blocks[0]
                    abs_start = idx + bstart
                    abs_end = idx + bend

                    date_m = re.search(r'<div class="phase-date">(.*?)</div>', inner, re.DOTALL)
                    desc_m = re.search(r'<div class="phase-desc">(.*?)</div>', inner, re.DOTALL)
                    hw_m = re.search(r'<div class="phase-hw">(.*?)</div>', inner, re.DOTALL)

                    parts = []
                    if date_m:
                        parts.append(f'**{self._inline_html(date_m.group(1)).strip()}**\n')
                    if desc_m:
                        desc = self._inline_html(desc_m.group(1)).strip()
                        parts.append(desc + '\n')
                    if hw_m:
                        hw = self._inline_html(hw_m.group(1)).strip()
                        parts.append(f'*{hw}*\n')

                    replacement = '\n' + '\n'.join(parts) + '\n'
                    s = s[:abs_start] + replacement + s[abs_end:]
                    search_from = abs_start + len(replacement)
                else:
                    search_from = idx + len(tag_prefix)
        return s

    def _convert_layer_diagram(self, s: str) -> str:
        """转换 <div class="layer-diagram"> → 表格。
        每个 layer-row 是一行，layer-cell 是一列。"""
        for start, end, inner in reversed(self._extract_block(s, '<div class="layer-diagram">')):
            rows = []
            for _, _, row_inner in self._extract_block(inner, '<div class="layer-row">'):
                cells = re.findall(r'<div class="layer-cell[^"]*">(.*?)</div>', row_inner)
                cells = [self._strip_tags(c).strip() for c in cells]
                if cells:
                    rows.append(cells)

            if rows:
                num_cols = max(len(r) for r in rows)
                header = '| ' + ' | '.join([f'L{i}' for i in range(num_cols)]) + ' |'
                sep = '|' + '|'.join(['---'] * num_cols) + '|'
                table_rows = []
                for row in rows:
                    # Highlight MLA cells
                    formatted = []
                    for c in row:
                        if 'MLA' in c:
                            formatted.append(f'**{c}**')
                        else:
                            formatted.append(c)
                    # Pad if needed
                    while len(formatted) < num_cols:
                        formatted.append('')
                    table_rows.append('| ' + ' | '.join(formatted) + ' |')
                replacement = '\n' + header + '\n' + sep + '\n' + '\n'.join(table_rows) + '\n'
            else:
                replacement = ''
            s = s[:start] + replacement + s[end:]
        return s

    def _convert_tree(self, s: str) -> str:
        """转换预格式化 div（tree、diagram 等）→ code block。
        保留树形字符（├── └── │）和 ASCII art 的等宽排版。"""
        # 所有需要转代码块的 class
        preformat_classes = [
            'tree', 'architecture-diagram', 'arch-diagram',
            'hero-ascii',
        ]
        for cls in preformat_classes:
            for start, end, inner in reversed(self._extract_block(s, f'<div class="{cls}">')):
                content = self._strip_tags(inner).strip()
                replacement = f'\n```\n{content}\n```\n'
                s = s[:start] + replacement + s[end:]
        return s

    def _convert_notes(self, s: str) -> str:
        """转换 <div class="note"> → Obsidian callout。"""
        def replace_note(m):
            classes = m.group(1)
            content = m.group(2).strip()
            # 清理内部 HTML
            content = self._inline_html(content)

            # Quartz callout 必须有 body 行（> 开头），否则 parser 会 crash
            callout_type = 'warning' if 'note-warn' in classes else 'info'
            # 将内容按行拆分，每行加 > 前缀
            body_lines = '\n'.join(f'> {line}' for line in content.split('\n') if line.strip())
            if not body_lines:
                body_lines = '> \u200b'  # zero-width space as minimum body
            return f'\n> [!{callout_type}]\n{body_lines}\n'

        return re.sub(r'<div class="(note[^"]*)">(.*?)</div>', replace_note, s, flags=re.DOTALL)

    def _convert_compare(self, s: str) -> str:
        """转换 <div class="compare"> → 两列表格。"""
        for start, end, inner in reversed(self._extract_block(s, '<div class="compare">')):
            # 提取两个子 div（用 _extract_block 处理嵌套）
            divs = self._extract_block(f'<div class="compare">{inner}</div>', '<div>')
            div_contents = [d[2] for d in divs]
            if len(div_contents) < 2:
                continue

            # 提取标题
            lt_m = re.search(r'<h3>(.*?)</h3>', div_contents[0])
            rt_m = re.search(r'<h3>(.*?)</h3>', div_contents[1])
            lt = lt_m.group(1) if lt_m else "A"
            rt = rt_m.group(1) if rt_m else "B"

            # 提取列表项
            left_items = re.findall(r'<li>[\s\-]*(.*?)</li>', div_contents[0], re.DOTALL)
            right_items = re.findall(r'<li>[\s\-]*(.*?)</li>', div_contents[1], re.DOTALL)

            max_len = max(len(left_items), len(right_items))
            rows = [f'| {lt} | {rt} |', '|------|------|']
            for i in range(max_len):
                l = self._inline_html(left_items[i]).strip() if i < len(left_items) else ''
                r = self._inline_html(right_items[i]).strip() if i < len(right_items) else ''
                rows.append(f'| {l} | {r} |')

            s = s[:start] + '\n' + '\n'.join(rows) + '\n' + s[end:]
        return s

    def _convert_stat_grid(self, s: str) -> str:
        """转换 <div class="grid"> 里的 stat cards → 单个紧凑 callout。
        每个 card 只有 card-title + card-value（+ 可选小字），合并为一个 callout。"""
        for start, end, inner in reversed(self._extract_block(s, '<div class="grid">')):
            # 提取所有 card 块
            cards = []
            for _, _, card_inner in self._extract_block(inner, '<div class="card">'):
                title_m = re.search(r'<div class="card-title">(.*?)</div>', card_inner)
                value_m = re.search(r'<div class="card-value"[^>]*>(.*?)</div>', card_inner)
                if title_m and value_m:
                    title = self._inline_html(title_m.group(1)).strip()
                    value = self._inline_html(value_m.group(1)).strip()
                    # 提取可选的小字说明
                    rest = card_inner
                    rest = rest.replace(title_m.group(0), '').replace(value_m.group(0), '')
                    rest = self._inline_html(self._strip_tags(rest)).strip()
                    cards.append((title, value, rest))

            if len(cards) >= 2:
                # 多个 stat cards → 合并为单个 callout
                lines = []
                for title, value, rest in cards:
                    detail = f'\n>     {rest}' if rest else ''
                    lines.append(f'> **{title}**\n> {value}{detail}')
                replacement = '\n> [!info]\n' + '\n>\n'.join(lines) + '\n'
                s = s[:start] + replacement + s[end:]
            # 如果只有 0-1 个 card，留给 _convert_cards 处理（移除 grid 包装）
            elif not cards:
                s = s[:start] + inner + s[end:]

        return s

    def _convert_cards(self, s: str) -> str:
        """转换 <div class="card"> → callout 或 heading+内容。
        Quartz 不支持 callout 内嵌 table/pre，这些情况用 heading + 裸内容。"""
        for start, end, inner in reversed(self._extract_block(s, '<div class="card">')):
            # 提取标题 — 支持 card-title div 或 h2/h3
            title = ''
            title_tag = ''
            title_match = re.search(r'<div class="card-title">(.*?)</div>', inner)
            if title_match:
                title = self._inline_html(title_match.group(1)).strip()
                title_tag = title_match.group(0)
            else:
                for htag in ['h2', 'h3', 'h4']:
                    h_match = re.search(rf'<{htag}[^>]*>(.*?)</{htag}>', inner)
                    if h_match:
                        title = self._inline_html(h_match.group(1)).strip()
                        title_tag = h_match.group(0)
                        break

            content = inner
            if title_tag:
                content = content.replace(title_tag, '')
            content = content.strip()

            # 简单 stat card（card-value 存在）→ 紧凑 callout
            value_match = re.search(r'<div class="card-value"[^>]*>(.*?)</div>', content)
            if value_match and title:
                value = self._inline_html(value_match.group(1)).strip()
                rest = content.replace(value_match.group(0), '').strip()
                rest = self._inline_html(self._strip_tags(rest)).strip()
                if rest:
                    replacement = f'\n> [!info] **{title}**\n> {value}\n>     {rest}\n'
                else:
                    replacement = f'\n> [!info] **{title}**\n> {value}\n'
                s = s[:start] + replacement + s[end:]
                continue

            has_table = '<table' in content
            has_pre = '<pre>' in content

            if has_table or has_pre:
                # Table/pre 不能放在 callout 里 — 用 heading + 裸内容
                # 不在这里转换 table/pre，留给后续 _convert_standard_html 处理
                replacement = f'\n### {title}\n\n{content}\n' if title else f'\n{content}\n'
            else:
                content = self._inline_html(content)
                body_lines = '\n'.join(f'> {line}' for line in content.split('\n') if line.strip())
                if not body_lines:
                    body_lines = '> \u200b'
                replacement = f'\n> [!info] {title}\n{body_lines}\n'

            s = s[:start] + replacement + s[end:]
        return s

    def _convert_bar_chart(self, s: str) -> str:
        """转换 <div class="bar-chart"> → Markdown 表格。
        每个 bar-group 有 bar-value（数值）、bar（柱体 title）、bar-label（标签）。"""
        for start, end, inner in reversed(self._extract_block(s, '<div class="bar-chart">')):
            # 提取所有 bar-group
            labels = []
            values = []
            for _, _, group_inner in self._extract_block(inner, '<div class="bar-group">'):
                value_m = re.search(r'<div class="bar-value[^"]*"[^>]*>(.*?)</div>', group_inner)
                label_m = re.search(r'<div class="bar-label">(.*?)</div>', group_inner, re.DOTALL)
                value = self._strip_tags(value_m.group(1)).strip() if value_m else ''
                raw_label = label_m.group(1) if label_m else ''
                # <br> → space before stripping tags
                raw_label = re.sub(r'<br\s*/?>', ' ', raw_label)
                label = self._strip_tags(raw_label).strip().replace('\n', ' ')
                labels.append(label)
                values.append(value)

            if labels:
                # 生成横向表格
                header = '| ' + ' | '.join(labels) + ' |'
                sep = '|' + '|'.join(['---'] * len(labels)) + '|'
                row = '| ' + ' | '.join(f'**{v}**' for v in values) + ' |'
                replacement = f'\n{header}\n{sep}\n{row}\n'
            else:
                replacement = ''
            s = s[:start] + replacement + s[end:]

        # 也移除 chart-container 包装
        for start, end, inner in reversed(self._extract_block(s, '<div class="chart-container">')):
            s = s[:start] + '\n' + inner.strip() + '\n' + s[end:]

        return s

    def _convert_param_bar(self, s: str) -> str:
        """转换 <div class="param-bar"> → 文字描述。"""
        def replace_bar(m):
            block = m.group(1)
            items = re.findall(r'title="([^"]*)"[^>]*>(.*?)</div>', block)
            if not items:
                return ''
            parts = []
            for title, text in items:
                label = text.strip() or title
                if label:
                    parts.append(label)
            return '\n**参数分布**: ' + ' | '.join(parts) + '\n'

        return re.sub(r'<div class="param-bar">(.*?)</div>', replace_bar, s, flags=re.DOTALL)

    _BOX_CHARS = re.compile(r'[┌┐└┘├┤┬┴┼─│▼▲►◄═╔╗╚╝╠╣╦╩╬]')

    def _convert_ascii_art_paragraphs(self, s: str) -> str:
        """检测 <p> 内含 box-drawing 字符的内容，转为 <pre><code> 保持等宽排版。"""
        def _replace(m):
            inner = m.group(1)
            # 至少包含 3 个 box-drawing 字符才算 ASCII art
            if len(self._BOX_CHARS.findall(inner)) >= 3:
                # 清理 HTML 内部的标签，保留纯文本
                text = re.sub(r'<br\s*/?>', '\n', inner)
                text = self._strip_tags(text)
                return f'<pre><code>{text}</code></pre>'
            return m.group(0)
        return re.sub(r'<p>(.*?)</p>', _replace, s, flags=re.DOTALL)

    def _convert_standard_html(self, s: str) -> str:
        """转换标准 HTML 标签为 Markdown。"""
        # Headers
        s = re.sub(r'<h1[^>]*>(.*?)</h1>', r'# \1', s)
        s = re.sub(r'<h2[^>]*>(.*?)</h2>', r'## \1', s)
        s = re.sub(r'<h3[^>]*>(.*?)</h3>', r'### \1', s)
        s = re.sub(r'<h4[^>]*>(.*?)</h4>', r'#### \1', s)

        # Table
        s = self._convert_tables(s)

        # Pre/code blocks (must be before inline <code> replacement)
        def replace_pre(m):
            code = m.group(1)
            code = self._strip_tags(code)
            return f'\n```\n{code}\n```\n'
        s = re.sub(r'<pre>(.*?)</pre>', replace_pre, s, flags=re.DOTALL)

        # Bold, italic, code
        s = re.sub(r'<strong>(.*?)</strong>', r'**\1**', s)
        s = re.sub(r'<b>(.*?)</b>', r'**\1**', s)
        s = re.sub(r'<em>(.*?)</em>', r'*\1*', s)
        s = re.sub(r'<i>(.*?)</i>', r'*\1*', s)
        s = re.sub(r'<code>(.*?)</code>', r'`\1`', s)

        # Links
        s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', s)

        # Lists
        s = re.sub(r'<li>(.*?)</li>', r'- \1', s, flags=re.DOTALL)
        s = re.sub(r'</?[ou]l[^>]*>', '', s)

        # Paragraphs
        s = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n', s, flags=re.DOTALL)

        # BR
        s = re.sub(r'<br\s*/?>', '\n', s)

        # HR
        s = re.sub(r'<hr\s*/?>', '\n---\n', s)

        return s

    def _convert_tables(self, s: str) -> str:
        """转换 HTML table → Markdown table。"""
        def replace_table(m):
            table_html = m.group(0)

            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
            if not rows:
                return table_html

            # Parse all rows into a 2D array
            all_rows = []
            for row in rows:
                cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL)
                cells = [self._inline_html(c).strip() for c in cells]
                all_rows.append(cells)

            if not all_rows:
                return table_html

            # Detect and remove empty columns (e.g. "Bar" column with only div placeholders)
            num_cols = max(len(r) for r in all_rows)
            empty_cols = set()
            for col_idx in range(num_cols):
                col_values = [r[col_idx] if col_idx < len(r) else '' for r in all_rows[1:]]  # skip header
                if all(v == '' for v in col_values):
                    empty_cols.add(col_idx)

            # Filter out empty columns
            if empty_cols:
                all_rows = [
                    [c for j, c in enumerate(row) if j not in empty_cols]
                    for row in all_rows
                ]

            md_rows = []
            for i, cells in enumerate(all_rows):
                md_rows.append('| ' + ' | '.join(cells) + ' |')
                # Add separator after header
                if i == 0:
                    md_rows.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')

            return '\n' + '\n'.join(md_rows) + '\n'

        return re.sub(r'<table[^>]*>.*?</table>', replace_table, s, flags=re.DOTALL)

    def _inline_html(self, s: str) -> str:
        """清理 inline HTML 为纯文本/Markdown。"""
        s = re.sub(r'<strong>(.*?)</strong>', r'**\1**', s)
        s = re.sub(r'<b>(.*?)</b>', r'**\1**', s)
        s = re.sub(r'<code>(.*?)</code>', r'`\1`', s)
        s = re.sub(r'<em>(.*?)</em>', r'*\1*', s)
        s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', s)
        s = re.sub(r'<span class="highlight">(.*?)</span>', r'**\1**', s)
        s = re.sub(r'<span class="dim">(.*?)</span>', r'\1', s)
        s = re.sub(r'<span class="badge[^"]*">(.*?)</span>', r'**\1**', s)
        s = re.sub(r'<span class="tag[^"]*">(.*?)</span>', r'**\1**', s)
        s = re.sub(r'<span class="explain-term" data-explain="([^"]*)">(.*?)</span>', r'[[\1|\2]]', s)
        s = re.sub(r'<span[^>]*>(.*?)</span>', r'\1', s, flags=re.DOTALL)
        s = re.sub(r'<br\s*/?>', ' ', s)
        s = self._strip_tags(s)
        return s

    def _strip_tags(self, s: str) -> str:
        """移除所有 HTML 标签。"""
        return re.sub(r'<[^>]+>', '', s)

    @staticmethod
    def _wrap_ascii_art(s: str) -> str:
        """检测未在代码块中的 box-drawing 字符行，自动包裹为代码块。
        避免 │ 被 Markdown 解释为表格分隔符。"""
        box_chars = set('┌┐└┘├┤┬┴┼│─═║╔╗╚╝╠╣╦╩╬')
        lines = s.split('\n')
        result = []
        in_code = False
        ascii_block = []

        def flush_ascii():
            if ascii_block:
                result.append('```')
                result.extend(ascii_block)
                result.append('```')
                ascii_block.clear()

        for line in lines:
            if line.strip().startswith('```'):
                flush_ascii()
                in_code = not in_code
                result.append(line)
                continue

            if in_code:
                result.append(line)
                continue

            # Check if line contains box-drawing characters
            has_box = any(c in box_chars for c in line)
            if has_box:
                ascii_block.append(line)
            else:
                flush_ascii()
                result.append(line)

        flush_ascii()
        return '\n'.join(result)

    def _cleanup(self, s: str) -> str:
        """清理最终输出。"""
        # 移除 HTML 注释（Quartz enableInHtmlEmbed 对注释节点会 NPE）
        s = re.sub(r'<!--.*?-->', '', s, flags=re.DOTALL)

        # HTML entities
        s = s.replace('&nbsp;', ' ')
        s = s.replace('&amp;', '&')
        s = s.replace('&lt;', '<')
        s = s.replace('&gt;', '>')
        s = s.replace('&#9889;', '⚡')
        s = s.replace('&#128295;', '🔧')
        s = s.replace('&#128640;', '🚀')
        s = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s)

        # 清理所有残留 span 标签（保留内容）
        s = re.sub(r'<span[^>]*>(.*?)</span>', r'\1', s, flags=re.DOTALL)

        # 移除残留的 div/section 标签
        s = re.sub(r'</?div[^>]*>', '', s)
        s = re.sub(r'</?section[^>]*>', '', s)
        s = re.sub(r'</?nav[^>]*>', '', s)
        s = re.sub(r'</?footer[^>]*>', '', s)
        s = re.sub(r'</?header[^>]*>', '', s)
        s = re.sub(r'</?main[^>]*>', '', s)
        s = re.sub(r'</?article[^>]*>', '', s)

        # 移除 svg-container div（SVG 已被提取）
        s = re.sub(r'<div class="svg-container">\s*', '\n', s)

        # 移除 style 标签
        s = re.sub(r'<style[^>]*>.*?</style>', '', s, flags=re.DOTALL)
        s = re.sub(r'<script[^>]*>.*?</script>', '', s, flags=re.DOTALL)

        # 移除 TOC div（Quartz 自带）
        s = re.sub(r'<div class="toc">.*?</div>', '', s, flags=re.DOTALL)

        # 移除 subtitle（已有 frontmatter description）
        s = re.sub(r'<p class="subtitle">.*?</p>', '', s, flags=re.DOTALL)

        # 移除 footer
        s = re.sub(r'<div class="footer">.*?</div>', '', s, flags=re.DOTALL)

        # 移除 dim 段落
        s = re.sub(r'<p class="dim"[^>]*>(.*?)</p>', r'*\1*', s, flags=re.DOTALL)

        # 清理多余空行
        s = re.sub(r'\n{4,}', '\n\n\n', s)

        # 自动包裹 ASCII art（box-drawing 字符）为代码块
        s = self._wrap_ascii_art(s)

        # 清理行首空格（保护代码块和 callout 缩进）
        lines = s.split('\n')
        cleaned = []
        in_code_block = False
        for line in lines:
            stripped = line.rstrip()
            if stripped.startswith('```'):
                in_code_block = not in_code_block
                cleaned.append(stripped)
            elif in_code_block:
                # 代码块内保留原始缩进
                cleaned.append(line.rstrip())
            elif stripped.startswith('>'):
                # callout 行保留 > 前缀后的缩进
                cleaned.append(stripped)
            else:
                # 普通行去掉前导空格（避免 4-space 变代码块）
                cleaned.append(stripped.lstrip())
        s = '\n'.join(cleaned)

        return s.strip()


def main():
    parser = argparse.ArgumentParser(description="HTML → Wiki Markdown 高保真转换")
    parser.add_argument("html_file", help="HTML 文件路径")
    parser.add_argument("--slug", help="输出 slug（默认从文件名推断）")
    parser.add_argument("--dry-run", action="store_true", help="只输出到 stdout，不写文件")
    parser.add_argument("--force", action="store_true", help="覆盖已有文件")

    args = parser.parse_args()

    html_path = Path(args.html_file)
    if not html_path.exists():
        print(f"错误: 文件不存在: {html_path}", file=sys.stderr)
        sys.exit(1)

    slug = args.slug or html_path.stem
    html = html_path.read_text(encoding="utf-8")

    # 推断 source URL
    source_url = f"{os.environ.get('CC_PAGES_URL_PREFIX','').rstrip('/')}/pages/{html_path.name}" if os.environ.get("CC_PAGES_URL_PREFIX") else ""

    # 转换
    converter = Html2MdConverter(html, source_url)
    body = converter.convert()

    if args.dry_run:
        print(body)
        return

    # 查找已有页面
    existing = find_page_by_slug(slug)
    if existing and not args.force:
        print(f"页面已存在: {existing}", file=sys.stderr)
        print(f"使用 --force 覆盖", file=sys.stderr)
        print(str(existing))
        return

    if existing:
        # 读取已有 frontmatter
        from wiki_utils import parse_frontmatter
        meta, _ = parse_frontmatter(existing)
        out_path = existing
    else:
        out_path = WIKI_CONTENT / "sources" / f"{slug}.md"
        meta = {}

    # 保留原有 frontmatter，只更新 body
    if existing:
        old_text = existing.read_text(encoding="utf-8")
        # 找到 frontmatter 结束位置
        fm_end = old_text.find('\n---', 3)
        if fm_end != -1:
            frontmatter = old_text[:fm_end + 4]
        else:
            frontmatter = ''
        new_content = frontmatter + '\n\n' + body + '\n'
    else:
        new_content = body

    out_path.write_text(new_content, encoding="utf-8")
    print(str(out_path))
    print(f"✅ 转换完成: {out_path.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
