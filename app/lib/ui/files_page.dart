import 'dart:io';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';

import '../core/models.dart';
import '../net/pc_client.dart';
import '../platform.dart';
import '../theme.dart';
import 'common.dart';

/// PC の共有フォルダ(作業フォルダ)のファイルを見る・受け取る・送る。
class FilesPage extends StatefulWidget {
  const FilesPage({super.key, required this.active});
  final bool active;

  @override
  State<FilesPage> createState() => _FilesPageState();
}

class _FilesPageState extends State<FilesPage> {
  String _path = '';
  String _root = '';
  List<Map<String, dynamic>> _entries = [];
  bool _loading = false;
  String? _error;
  final Map<String, double> _transfers = {};
  bool _loadedOnce = false;

  @override
  void didUpdateWidget(FilesPage old) {
    super.didUpdateWidget(old);
    if (widget.active && !_loadedOnce) _load(_path);
  }

  Future<void> _load(String path) async {
    final state = AppScope.read(context);
    if (!state.online) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final r = await state.client!.getJson('/api/files', query: {'path': path});
      setState(() {
        _path = r['path'] as String? ?? path;
        _root = r['root'] as String? ?? '';
        _entries = [for (final e in r['entries'] as List) Map<String, dynamic>.from(e as Map)];
        _loadedOnce = true;
      });
    } catch (e) {
      setState(() => _error = describeError(e));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _download(Map<String, dynamic> entry) async {
    final state = AppScope.read(context);
    final name = entry['name'] as String;
    final tmp = await getTemporaryDirectory();
    final dest = File('${tmp.path}/$name');
    setState(() => _transfers[name] = 0);
    try {
      await state.client!.download('/api/files/raw', {'path': entry['path'] as String}, dest, onProgress: (r, t) {
        if (t > 0 && mounted) setState(() => _transfers[name] = r / t);
      });
      final saved = await PlatformBridge.saveToDownloads(dest.path, name);
      state.toast('「ダウンロード」に保存しました${saved != null && saved != dest.path ? '' : ''}: $name');
      if (await dest.exists() && Platform.isAndroid) await dest.delete();
    } catch (e) {
      state.toast(describeError(e));
    } finally {
      if (mounted) setState(() => _transfers.remove(name));
    }
  }

  Future<void> _upload() async {
    final state = AppScope.read(context);
    final picked = await PlatformBridge.pickFile();
    if (picked == null) return;
    final remote = _path.isEmpty ? picked.name : '$_path/${picked.name}';
    setState(() => _transfers[picked.name] = 0);
    try {
      await state.client!.upload(remote, File(picked.path), onProgress: (s, t) {
        if (t > 0 && mounted) setState(() => _transfers[picked.name] = s / t);
      });
      state.toast('PC に送りました: ${picked.name}');
      await _load(_path);
    } catch (e) {
      state.toast(describeError(e));
    } finally {
      if (mounted) setState(() => _transfers.remove(picked.name));
      try {
        await File(picked.path).delete();
      } catch (_) {}
    }
  }

  Future<void> _mkdir() async {
    final state = AppScope.read(context);
    final name = await askText(context, title: '新しいフォルダ', hint: 'フォルダの名前', ok: '作成');
    if (name == null || name.trim().isEmpty) return;
    try {
      await state.client!.postJson('/api/files/mkdir', {'path': _path.isEmpty ? name.trim() : '$_path/${name.trim()}'});
      await _load(_path);
    } catch (e) {
      state.toast(describeError(e));
    }
  }

  Future<void> _delete(Map<String, dynamic> entry) async {
    final state = AppScope.read(context);
    if (!await confirm(context, title: '削除しますか?', body: '${entry['name']}\nPC のバックアップに移すので、あとから戻せます。', ok: '削除', danger: true)) return;
    try {
      await state.client!.postJson('/api/files/delete', {'path': entry['path']});
      await _load(_path);
    } catch (e) {
      state.toast(describeError(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    if (!state.online) {
      return Scaffold(
        appBar: AppBar(title: const Text('ファイル')),
        body: EmptyState(
          icon: Icons.folder_off_outlined,
          title: 'PC につながっていません',
          body: 'PC の共有フォルダのファイルを見たり、受け取ったり、送ったりできます。',
          action: state.paired ? FilledButton(onPressed: state.connect, child: const Text('再接続')) : null,
        ),
      );
    }
    if (!_loadedOnce && !_loading && widget.active) WidgetsBinding.instance.addPostFrameCallback((_) => _load(_path));
    final crumbs = _path.isEmpty ? <String>[] : _path.split('/');
    return Scaffold(
      appBar: AppBar(
        title: const Text('ファイル'),
        actions: [
          IconButton(tooltip: 'フォルダを作成', onPressed: _mkdir, icon: const Icon(Icons.create_new_folder_outlined)),
          IconButton(tooltip: '更新', onPressed: () => _load(_path), icon: const Icon(Icons.refresh)),
        ],
      ),
      floatingActionButton: Platform.isAndroid
          ? FloatingActionButton.extended(onPressed: _upload, icon: const Icon(Icons.upload_file), label: const Text('PC に送る'))
          : null,
      body: Column(children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
          child: Row(children: [
            ActionChip(avatar: const Icon(Icons.home_outlined, size: 16), label: const Text('共有フォルダ'), onPressed: () => _load('')),
            for (var i = 0; i < crumbs.length; i++) ...[
              Icon(Icons.chevron_right, size: 18, color: p.muted),
              ActionChip(label: Text(crumbs[i]), onPressed: () => _load(crumbs.take(i + 1).join('/'))),
            ],
          ]),
        ),
        if (_root.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Align(alignment: Alignment.centerLeft, child: Text('PC の場所: $_root', style: TextStyle(fontSize: 11.5, color: p.muted))),
          ),
        if (_loading) const LinearProgressIndicator(minHeight: 2),
        if (_error != null) Padding(padding: const EdgeInsets.all(16), child: Text(_error!, style: TextStyle(color: p.bad))),
        for (final t in _transfers.entries)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
            child: Row(children: [
              Expanded(child: Text(t.key, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13))),
              SizedBox(width: 120, child: LinearProgressIndicator(value: t.value)),
            ]),
          ),
        Expanded(
          child: _entries.isEmpty && !_loading
              ? Center(child: Text('空のフォルダです', style: TextStyle(color: p.muted)))
              : ListView.builder(
                  padding: const EdgeInsets.fromLTRB(8, 4, 8, 96),
                  itemCount: _entries.length,
                  itemBuilder: (context, i) {
                    final e = _entries[i];
                    final dir = e['dir'] == true;
                    return ListTile(
                      leading: Icon(dir ? Icons.folder_outlined : _iconFor(e['name'] as String), color: dir ? p.gold : context.colors.primary),
                      title: Text(e['name'] as String, maxLines: 1, overflow: TextOverflow.ellipsis),
                      subtitle: Text(dir ? 'フォルダ' : '${formatBytes(e['size'] as num)}・${shortTime((e['modified'] as num).toDouble())}',
                          style: TextStyle(color: p.muted, fontSize: 12)),
                      onTap: () => dir ? _load(e['path'] as String) : _download(e),
                      trailing: dir
                          ? Icon(Icons.chevron_right, color: p.muted)
                          : PopupMenuButton<String>(
                              onSelected: (v) => v == 'get' ? _download(e) : _delete(e),
                              itemBuilder: (_) => const [
                                PopupMenuItem(value: 'get', child: Text('スマホに保存')),
                                PopupMenuItem(value: 'delete', child: Text('削除')),
                              ],
                            ),
                    );
                  },
                ),
        ),
      ]),
    );
  }

  IconData _iconFor(String name) {
    final n = name.toLowerCase();
    if (RegExp(r'\.(png|jpe?g|gif|webp|bmp|heic)$').hasMatch(n)) return Icons.image_outlined;
    if (RegExp(r'\.(mp4|mov|mkv|avi|webm)$').hasMatch(n)) return Icons.movie_outlined;
    if (RegExp(r'\.(mp3|wav|flac|m4a|ogg)$').hasMatch(n)) return Icons.music_note_outlined;
    if (RegExp(r'\.(py|js|ts|dart|kt|java|c|cpp|h|rs|go|html|css|json|ya?ml|toml|sh|ps1)$').hasMatch(n)) return Icons.code;
    if (RegExp(r'\.(zip|7z|rar|tar|gz)$').hasMatch(n)) return Icons.folder_zip_outlined;
    if (RegExp(r'\.(pdf|docx?|xlsx?|pptx?|txt|md|csv)$').hasMatch(n)) return Icons.description_outlined;
    return Icons.insert_drive_file_outlined;
  }
}
