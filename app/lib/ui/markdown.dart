import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/models.dart';
import '../platform.dart';
import '../theme.dart';

/// 回答の Markdown を表示する(見出し・箇条書き・表・コードブロック・太字・インラインコード・リンク)。
class MarkdownView extends StatelessWidget {
  const MarkdownView(this.text, {super.key, this.selectable = true});
  final String text;
  final bool selectable;

  @override
  Widget build(BuildContext context) {
    final blocks = _parse(text);
    final column = Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      for (final b in blocks) Padding(padding: const EdgeInsets.only(bottom: 8), child: _block(context, b)),
    ]);
    return selectable ? SelectionArea(child: column) : column;
  }

  Widget _block(BuildContext context, _Block b) {
    final theme = Theme.of(context);
    switch (b.kind) {
      case _Kind.code:
        return _CodeBlock(code: b.text, language: b.lang);
      case _Kind.heading:
        return Text.rich(_inline(context, b.text, theme.textTheme.titleMedium!.copyWith(fontWeight: FontWeight.w700)));
      case _Kind.list:
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          for (var i = 0; i < b.items.length; i++)
            Padding(
              padding: const EdgeInsets.only(bottom: 3),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                SizedBox(
                  width: 24,
                  child: Text(b.ordered ? '${i + 1}.' : '•', style: TextStyle(color: context.palette.muted, height: 1.6)),
                ),
                Expanded(child: Text.rich(_inline(context, b.items[i], theme.textTheme.bodyMedium!))),
              ]),
            ),
        ]);
      case _Kind.table:
        return SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Table(
            defaultColumnWidth: const IntrinsicColumnWidth(),
            border: TableBorder.all(color: context.palette.line),
            children: [
              for (var r = 0; r < b.rows.length; r++)
                TableRow(
                  decoration: r == 0 ? BoxDecoration(color: context.palette.sunken) : null,
                  children: [
                    for (final cell in b.rows[r])
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                        child: Text.rich(_inline(context, cell, theme.textTheme.bodyMedium!.copyWith(fontWeight: r == 0 ? FontWeight.w600 : null))),
                      ),
                  ],
                ),
            ],
          ),
        );
      case _Kind.paragraph:
        return Text.rich(_inline(context, b.text, theme.textTheme.bodyMedium!.copyWith(fontSize: 15)));
    }
  }

  static final _inlinePattern = RegExp(r'(\*\*[^*]+\*\*)|(`[^`]+`)|(https?://[^\s<>()]+)');

  TextSpan _inline(BuildContext context, String text, TextStyle style) {
    final spans = <InlineSpan>[];
    var index = 0;
    for (final m in _inlinePattern.allMatches(text)) {
      if (m.start > index) spans.add(TextSpan(text: text.substring(index, m.start)));
      final token = m.group(0)!;
      if (m.group(1) != null) {
        spans.add(TextSpan(text: token.substring(2, token.length - 2), style: const TextStyle(fontWeight: FontWeight.w700)));
      } else if (m.group(2) != null) {
        spans.add(TextSpan(
          text: token.substring(1, token.length - 1),
          style: TextStyle(fontFamily: 'monospace', fontSize: style.fontSize != null ? style.fontSize! * 0.92 : 13.5, backgroundColor: context.palette.sunken),
        ));
      } else {
        spans.add(TextSpan(
          text: token,
          style: TextStyle(color: context.colors.primary, decoration: TextDecoration.underline),
          recognizer: TapGestureRecognizer()..onTap = () => PlatformBridge.openUrl(token),
        ));
      }
      index = m.end;
    }
    if (index < text.length) spans.add(TextSpan(text: text.substring(index)));
    return TextSpan(style: style, children: spans);
  }

  static List<_Block> _parse(String text) {
    final blocks = <_Block>[];
    final parts = pipeTables(text).split('```');
    for (var i = 0; i < parts.length; i++) {
      final part = parts[i];
      if (i.isOdd) {
        final newline = part.indexOf('\n');
        final lang = newline < 0 ? '' : part.substring(0, newline).trim();
        var body = newline < 0 ? part : part.substring(newline + 1);
        if (body.endsWith('\n')) body = body.substring(0, body.length - 1);
        blocks.add(_Block.code(body, lang));
        continue;
      }
      final para = <String>[];
      _Block? list;
      final table = <List<String>>[];
      void flush() {
        if (para.isNotEmpty) {
          blocks.add(_Block.paragraph(para.join('\n')));
          para.clear();
        }
        if (table.isNotEmpty) {
          blocks.add(_Block.table(List.of(table)));
          table.clear();
        }
        list = null;
      }

      for (final raw in part.split('\n')) {
        final line = raw.trimRight();
        if (RegExp(r'^\s*\|.*\|\s*$').hasMatch(line)) {
          if (para.isNotEmpty || list != null) flush();
          if (table.length == 1 && tableDelimiter.hasMatch(line)) continue; // 見出しの下の区切りの行(|---|---|)
          table.add(tableCells(line));
          continue;
        }
        if (table.isNotEmpty) flush();
        final head = RegExp(r'^#{1,4}\s+(.*)').firstMatch(line);
        final item = RegExp(r'^\s*(?:[-*・]|(\d+)[.)])\s+(.*)').firstMatch(line);
        if (head != null) {
          flush();
          blocks.add(_Block.heading(head.group(1)!));
        } else if (item != null) {
          if (para.isNotEmpty) {
            blocks.add(_Block.paragraph(para.join('\n')));
            para.clear();
          }
          final ordered = item.group(1) != null;
          if (list == null || list!.ordered != ordered) {
            list = _Block.list(ordered);
            blocks.add(list!);
          }
          list!.items.add(item.group(2)!);
        } else if (line.trim().isEmpty) {
          flush();
        } else {
          list = null;
          para.add(line);
        }
      }
      flush();
    }
    return blocks;
  }
}

enum _Kind { paragraph, heading, list, code, table }

class _Block {
  _Block._(this.kind, {this.text = '', this.lang = '', this.ordered = false, List<List<String>>? rows}) : rows = rows ?? [];
  factory _Block.paragraph(String t) => _Block._(_Kind.paragraph, text: t);
  factory _Block.heading(String t) => _Block._(_Kind.heading, text: t);
  factory _Block.code(String t, String lang) => _Block._(_Kind.code, text: t, lang: lang);
  factory _Block.list(bool ordered) => _Block._(_Kind.list, ordered: ordered);
  factory _Block.table(List<List<String>> rows) => _Block._(_Kind.table, rows: rows);

  final _Kind kind;
  final String text;
  final String lang;
  final bool ordered;
  final List<String> items = [];
  final List<List<String>> rows;
}

class _CodeBlock extends StatefulWidget {
  const _CodeBlock({required this.code, required this.language});
  final String code;
  final String language;

  @override
  State<_CodeBlock> createState() => _CodeBlockState();
}

class _CodeBlockState extends State<_CodeBlock> {
  bool copied = false;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Container(
      decoration: BoxDecoration(color: p.sunken, borderRadius: BorderRadius.circular(12), border: Border.all(color: p.line)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Container(
          padding: const EdgeInsets.fromLTRB(12, 4, 4, 4),
          decoration: BoxDecoration(border: Border(bottom: BorderSide(color: p.line))),
          child: Row(children: [
            Text(widget.language.isEmpty ? 'コード' : widget.language, style: TextStyle(fontSize: 12, color: p.muted)),
            const Spacer(),
            TextButton.icon(
              style: TextButton.styleFrom(visualDensity: VisualDensity.compact, foregroundColor: p.muted),
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: widget.code));
                setState(() => copied = true);
                Future.delayed(const Duration(seconds: 2), () {
                  if (mounted) setState(() => copied = false);
                });
              },
              icon: Icon(copied ? Icons.check : Icons.content_copy, size: 15),
              label: Text(copied ? 'コピーしました' : 'コピー', style: const TextStyle(fontSize: 12)),
            ),
          ]),
        ),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.all(12),
          child: Text(widget.code, style: const TextStyle(fontFamily: 'monospace', fontSize: 13, height: 1.5)),
        ),
      ]),
    );
  }
}
