import 'dart:io';

import 'package:celestial_llm/celestial_llm.dart';
import 'package:flutter/material.dart';

import '../ai/local_ai.dart';
import '../core/models.dart';
import '../net/pc_client.dart';
import '../theme.dart';
import 'common.dart';

/// 端末内AIのモデル。PC から受け取り、スマホの中だけで動かす。
class ModelsPage extends StatefulWidget {
  const ModelsPage({super.key, required this.active});
  final bool active;

  @override
  State<ModelsPage> createState() => _ModelsPageState();
}

class _ModelsPageState extends State<ModelsPage> {
  List<RemoteModel> _remote = [];
  String? _error;
  bool _loading = false;
  bool _loaded = false;
  String? _benchResult;
  bool _benching = false;

  @override
  void didUpdateWidget(ModelsPage old) {
    super.didUpdateWidget(old);
    if (widget.active && !_loaded) _loadRemote();
  }

  Future<void> _loadRemote() async {
    final state = AppScope.read(context);
    if (!state.online) return;
    setState(() => _loading = true);
    try {
      final r = await state.client!.getJson('/api/phone/models');
      setState(() {
        _remote = [for (final m in r['models'] as List) RemoteModel(Map<String, dynamic>.from(m as Map))];
        _error = null;
        _loaded = true;
      });
    } catch (e) {
      setState(() => _error = describeError(e));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _bench() async {
    final state = AppScope.read(context);
    final name = state.settings.localModel;
    if (name.isEmpty) return;
    setState(() {
      _benching = true;
      _benchResult = null;
    });
    try {
      final watch = Stopwatch()..start();
      final buffer = StringBuffer();
      await for (final piece in state.ai.generate(name, const [LlmMessage('user', '日本の四季をそれぞれ一文で説明してください。')],
          options: const LlmOptions(temperature: 0.2, maxTokens: 128), contextSize: state.settings.contextSize, threads: state.settings.threads)) {
        buffer.write(piece);
      }
      watch.stop();
      setState(() => _benchResult = '生成 ${state.ai.lastSpeed.toStringAsFixed(1)} トークン/秒、入力の読み込み ${state.ai.lastPromptMs} ms(合計 ${(watch.elapsedMilliseconds / 1000).toStringAsFixed(1)} 秒)');
    } catch (e) {
      setState(() => _benchResult = 'エラー: $e');
    } finally {
      if (mounted) setState(() => _benching = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final ai = state.ai;
    final p = context.palette;
    if (!ai.supported) {
      return const EmptyState(icon: Icons.memory_outlined, title: '端末内のAIは Android 用です', body: 'PC では Ollama のモデルを使います。');
    }
    if (widget.active && !_loaded && !_loading && state.online) WidgetsBinding.instance.addPostFrameCallback((_) => _loadRemote());
    return ListenableBuilder(
      listenable: ai,
      builder: (context, _) => Scaffold(
        appBar: AppBar(title: const Text('AIモデル'), actions: [IconButton(tooltip: '更新', onPressed: _loadRemote, icon: const Icon(Icons.refresh))]),
        body: ListView(padding: const EdgeInsets.fromLTRB(14, 4, 14, 24), children: [
          SectionCard(
            title: 'この端末',
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              _row(context, 'エンジン', 'llama.cpp b12319・${ai.variantLabel}'),
              _row(context, 'CPU コア', '${Platform.numberOfProcessors} 個(生成に ${state.settings.threads > 0 ? state.settings.threads : recommendedThreads()} 個を使用)'),
              _row(context, '読み込み中のAI', ai.loading ? '読み込み中…' : (ai.loadedName ?? 'なし')),
              if (ai.lastSpeed > 0) _row(context, '直近の速さ', '${ai.lastSpeed.toStringAsFixed(1)} トークン/秒(会話の前半 ${ai.lastReused} トークンを再利用)'),
              if (ai.loadError != null) Text(ai.loadError!, style: TextStyle(color: p.bad)),
            ]),
          ),
          const SizedBox(height: 12),
          SectionCard(
            title: '入っているAI',
            subtitle: 'スマホの中だけで動き、通信しません',
            child: ai.installed.isEmpty
                ? Text(state.online ? '下の一覧から受け取ってください。' : 'PC につながると、PC からAIを受け取れます。', style: TextStyle(color: p.muted))
                : RadioGroup<String>(
                    groupValue: state.settings.localModel,
                    onChanged: (v) => state.settings.update((s) => s.localModel = v ?? ''),
                    child: Column(children: [
                      for (final m in ai.installed)
                        ListTile(
                          contentPadding: EdgeInsets.zero,
                          leading: Radio<String>(value: m.name),
                          title: Text(m.name),
                          subtitle: Text('${formatBytes(m.size)}・${m.description}', style: TextStyle(color: p.muted, fontSize: 12)),
                          onTap: () => state.settings.update((s) => s.localModel = m.name),
                          trailing: IconButton(
                            tooltip: '削除',
                            icon: const Icon(Icons.delete_outline),
                            onPressed: () async {
                              if (await confirm(context, title: '${m.name} を削除しますか?', body: 'もう一度使うには、PC から受け取り直す必要があります。', ok: '削除', danger: true)) {
                                await ai.delete(m);
                                if (state.settings.localModel == m.name) {
                                  state.settings.update((s) => s.localModel = ai.installed.isNotEmpty ? ai.installed.first.name : '');
                                }
                              }
                            },
                          ),
                        ),
                      const SizedBox(height: 8),
                      Row(children: [
                        OutlinedButton.icon(
                          onPressed: _benching || state.settings.localModel.isEmpty ? null : _bench,
                          icon: _benching ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.speed, size: 18),
                          label: const Text('速さを測る'),
                        ),
                        const SizedBox(width: 8),
                        if (ai.loadedName != null) TextButton(onPressed: ai.unload, child: const Text('メモリから降ろす')),
                      ]),
                      if (_benchResult != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_benchResult!, style: TextStyle(color: p.muted))),
                    ]),
                  ),
          ),
          const SizedBox(height: 12),
          SectionCard(
            title: 'PC から受け取れるAI',
            subtitle: 'Tailscale 経由で PC から受け取ります。途中で切れても続きから受け取れます',
            child: !state.online
                ? Text('PC につながっていません。', style: TextStyle(color: p.muted))
                : _loading && _remote.isEmpty
                    ? const LinearProgressIndicator()
                    : Column(children: [
                        if (_error != null) Text(_error!, style: TextStyle(color: p.bad)),
                        for (final m in _remote) _remoteTile(context, m),
                      ]),
          ),
          const SizedBox(height: 12),
          SectionCard(
            title: '動かし方',
            child: Column(children: [
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('PC につながっていれば PC で答える'),
                subtitle: const Text('オフにすると、つながっていてもスマホ内のAIを先に使います'),
                value: state.settings.preferPc,
                onChanged: (v) => state.settings.update((s) => s.preferPc = v),
              ),
              _dropdown<int>(context, '会話の長さの上限', state.settings.contextSize, {2048: '2048 トークン(省メモリ)', 4096: '4096 トークン', 8192: '8192 トークン(長い会話)'},
                  (v) async {
                state.settings.update((s) => s.contextSize = v);
                await ai.unload();
              }),
              _dropdown<int>(context, '使う CPU コア', state.settings.threads, {0: '自動', 2: '2 個', 4: '4 個', 6: '6 個', 8: '8 個'}, (v) async {
                state.settings.update((s) => s.threads = v);
                await ai.unload();
              }),
              Row(children: [
                const Expanded(child: Text('回答のばらつき')),
                SizedBox(
                  width: 180,
                  child: Slider(
                    value: state.settings.temperature,
                    min: 0,
                    max: 1.2,
                    divisions: 12,
                    label: state.settings.temperature.toStringAsFixed(1),
                    onChanged: (v) => state.settings.update((s) => s.temperature = v),
                  ),
                ),
              ]),
            ]),
          ),
        ]),
      ),
    );
  }

  Widget _remoteTile(BuildContext context, RemoteModel m) {
    final state = AppScope.of(context);
    final ai = state.ai;
    final p = context.palette;
    final progress = ai.downloads[m.name];
    final installed = ai.find(m.name) != null;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Wrap(spacing: 6, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
                Text(m.name, style: const TextStyle(fontWeight: FontWeight.w600)),
                if (m.recommended) const Pill('おすすめ', tone: Tone.gold),
                if (m.own) const Pill('自作', tone: Tone.accent),
              ]),
              const SizedBox(height: 2),
              Text('${formatBytes(m.fileSize)}・${m.family} ${m.parameters} ${m.quantization}${m.accuracy != null ? '・計測の正解率 ${(m.accuracy! * 100).round()}%' : ''}',
                  style: TextStyle(fontSize: 12, color: p.muted)),
            ]),
          ),
          if (installed)
            const Pill('入っています', tone: Tone.good)
          else if (progress != null)
            TextButton(onPressed: () => ai.cancelDownload(m.name), child: const Text('中止'))
          else
            FilledButton.tonal(onPressed: () => ai.download(state.client!, m).then((_) {
                  if (state.settings.localModel.isEmpty && ai.find(m.name) != null) state.settings.update((s) => s.localModel = m.name);
                }), child: const Text('受け取る')),
        ]),
        if (progress != null) Padding(padding: const EdgeInsets.only(top: 6), child: LinearProgressIndicator(value: progress)),
        if (progress != null) Text('${(progress * 100).toStringAsFixed(0)}%', style: TextStyle(fontSize: 11.5, color: p.muted)),
        if (ai.downloadErrors[m.name] != null) Text(ai.downloadErrors[m.name]!, style: TextStyle(color: p.bad, fontSize: 12.5)),
      ]),
    );
  }

  Widget _row(BuildContext context, String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(width: 110, child: Text(k, style: TextStyle(color: context.palette.muted, fontSize: 13))),
          Expanded(child: Text(v, style: const TextStyle(fontSize: 13.5))),
        ]),
      );

  Widget _dropdown<T>(BuildContext context, String label, T value, Map<T, String> options, ValueChanged<T> onChanged) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(children: [
          Expanded(child: Text(label)),
          DropdownButton<T>(
            value: options.containsKey(value) ? value : options.keys.first,
            underline: const SizedBox(),
            items: [for (final e in options.entries) DropdownMenuItem(value: e.key, child: Text(e.value))],
            onChanged: (v) {
              if (v != null) onChanged(v);
            },
          ),
        ]),
      );
}
