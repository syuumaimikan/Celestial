import 'package:flutter/material.dart';

import '../app_state.dart';
import '../net/pc_client.dart';
import '../platform.dart';
import '../theme.dart';
import 'common.dart';

/// 自動化(PC で動くワークフロー)。スマホからは、動かす・オンオフ・記録を見る(作る・変えるのは PC の画面)。
const _triggerIcons = {
  'manual': Icons.play_arrow_rounded,
  'schedule': Icons.schedule,
  'interval': Icons.autorenew,
  'webhook': Icons.webhook,
  'file': Icons.folder_open_outlined,
};
const _stepIcons = {
  'ai': Icons.auto_awesome_outlined,
  'decide': Icons.balance,
  'fetch': Icons.public,
  'post': Icons.send_outlined,
  'read_file': Icons.description_outlined,
  'write_file': Icons.edit_note,
  'document': Icons.article_outlined,
  'command': Icons.terminal,
  'notify': Icons.notifications_none,
  'condition': Icons.call_split,
  'template': Icons.notes,
  'wait': Icons.hourglass_empty,
};
const _status = {
  'done': ('成功', Tone.good),
  'error': ('失敗', Tone.bad),
  'stopped': ('条件で停止', Tone.neutral),
  'running': ('実行中', Tone.accent),
};

String _when(num? ts) {
  if (ts == null) return '';
  final d = DateTime.fromMillisecondsSinceEpoch((ts * 1000).round());
  final now = DateTime.now();
  final hm = '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';
  return d.year == now.year && d.month == now.month && d.day == now.day ? hm : '${d.month}/${d.day} $hm';
}

/// 自動化の一覧を読み、変わったら読み直す(一覧ページと、PC操作のカードで使う)。
mixin _FlowsLoader<T extends StatefulWidget> on State<T> {
  Map<String, dynamic>? data;
  String? error;
  int _version = -1;

  void maybeLoad(AppState state) {
    if (!state.online || _version == state.flowsVersion) return;
    _version = state.flowsVersion;
    state.flows().then((d) {
      if (mounted) setState(() => (data = d, error = null));
    }).catchError((Object e) {
      if (mounted) setState(() => error = describeError(e));
    });
  }

  List<Map<String, dynamic>> get flows => [for (final f in (data?['flows'] as List? ?? const [])) Map<String, dynamic>.from(f as Map)];
}

class _FlowTile extends StatelessWidget {
  const _FlowTile({required this.flow});
  final Map<String, dynamic> flow;

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final p = context.palette;
    final trigger = Map<String, dynamic>.from(flow['trigger'] as Map);
    final last = flow['last_run'] == null ? null : Map<String, dynamic>.from(flow['last_run'] as Map);
    final status = _status[last?['status']];
    final steps = [for (final s in flow['steps'] as List) (s as Map)['type'] as String];
    return InkWell(
      borderRadius: BorderRadius.circular(12),
      onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => FlowRunsPage(flow: flow))),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(children: [
          Container(
            width: 38,
            height: 38,
            decoration: BoxDecoration(color: p.sunken, borderRadius: BorderRadius.circular(10), border: Border.all(color: p.line)),
            child: Icon(_triggerIcons[trigger['type']] ?? Icons.bolt, size: 20, color: context.colors.primary),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(flow['name'] as String? ?? '', style: const TextStyle(fontWeight: FontWeight.w600), maxLines: 1, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 3),
              Row(children: [
                Flexible(child: Text(flow['summary'] as String? ?? '', style: TextStyle(color: p.muted, fontSize: 12.5), maxLines: 1, overflow: TextOverflow.ellipsis)),
                const SizedBox(width: 6),
                for (final s in steps.take(6)) Padding(padding: const EdgeInsets.only(right: 2), child: Icon(_stepIcons[s] ?? Icons.circle, size: 13, color: p.muted)),
              ]),
              if (status != null || flow['running'] == true) ...[
                const SizedBox(height: 4),
                Pill(flow['running'] == true ? '実行中' : '${status!.$1}・${_when(last?['started'] as num?)}', tone: flow['running'] == true ? Tone.accent : status!.$2),
              ],
            ]),
          ),
          Switch(value: flow['enabled'] == true, onChanged: (v) => state.setFlowEnabled(flow['id'] as String, v)),
          IconButton.filledTonal(
            tooltip: '今すぐ動かす',
            onPressed: () => state.runFlow(flow['id'] as String, flow['name'] as String? ?? ''),
            icon: const Icon(Icons.play_arrow_rounded),
          ),
        ]),
      ),
    );
  }
}

/// 「PC操作」の画面に置く、自動化のカード(よく使う流れをすぐ動かせる)。
class FlowsCard extends StatefulWidget {
  const FlowsCard({super.key});

  @override
  State<FlowsCard> createState() => _FlowsCardState();
}

class _FlowsCardState extends State<FlowsCard> with _FlowsLoader {
  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    maybeLoad(state);
    final p = context.palette;
    final list = flows;
    return SectionCard(
      title: '自動化',
      subtitle: 'PC で動くワークフロー',
      trailing: TextButton(
        onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const FlowsPage())),
        child: const Text('すべて見る'),
      ),
      child: error != null
          ? Text(error!, style: TextStyle(color: p.bad))
          : data == null
              ? const LinearProgressIndicator()
              : list.isEmpty
                  ? Text('まだありません。PC の画面の「自動化」で作れます(AIに説明して作ることもできます)。', style: TextStyle(color: p.muted, height: 1.5))
                  : Column(children: [for (final f in list.take(3)) _FlowTile(flow: f)]),
    );
  }
}

/// 自動化の一覧(すべて)。
class FlowsPage extends StatefulWidget {
  const FlowsPage({super.key});

  @override
  State<FlowsPage> createState() => _FlowsPageState();
}

class _FlowsPageState extends State<FlowsPage> with _FlowsLoader {
  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    maybeLoad(state);
    final p = context.palette;
    final list = flows;
    final canEdit = data?['can_edit'] == true;
    return Scaffold(
      appBar: AppBar(title: const Text('自動化'), actions: [
        if (state.isDesktop)
          TextButton.icon(
            onPressed: () => PlatformBridge.openUrl('http://127.0.0.1:8765/'),
            icon: const Icon(Icons.open_in_new, size: 18),
            label: const Text('管理画面で作る・変える'),
          ),
      ]),
      body: !state.online
          ? EmptyState(icon: Icons.link_off, title: 'PC につながっていません', body: state.linkError ?? '自動化は PC で動きます。PC とつながると使えます。')
          : RefreshIndicator(
              onRefresh: () async {
                setState(() => (data = null));
                maybeLoad(state..flowsVersion += 1);
              },
              child: ListView(padding: const EdgeInsets.fromLTRB(14, 6, 14, 24), children: [
                Text(
                  canEdit
                      ? '作る・変えるのは、管理画面(ブラウザ)の「自動化」で行います。ここでは動かす・止める・記録を見ることができます。'
                      : '作る・変えるのは PC の画面で行います。スマホからは、動かす・止める・記録を見ることができます。',
                  style: TextStyle(color: p.muted, fontSize: 12.5, height: 1.5),
                ),
                const SizedBox(height: 8),
                if (error != null) Text(error!, style: TextStyle(color: p.bad)),
                if (data == null && error == null) const Padding(padding: EdgeInsets.all(24), child: Center(child: CircularProgressIndicator())),
                if (data != null && list.isEmpty)
                  const EmptyState(icon: Icons.account_tree_outlined, title: 'まだありません', body: 'PC の画面の「自動化」で、きっかけと手順をつないで作れます。'),
                for (final f in list) _FlowTile(flow: f),
              ]),
            ),
    );
  }
}

/// ある自動化の実行の記録(手順ごとの結果)。
class FlowRunsPage extends StatefulWidget {
  const FlowRunsPage({super.key, required this.flow});
  final Map<String, dynamic> flow;

  @override
  State<FlowRunsPage> createState() => _FlowRunsPageState();
}

class _FlowRunsPageState extends State<FlowRunsPage> {
  List<Map<String, dynamic>>? _runs;
  String? _error;
  int _version = -1;

  void _load(AppState state) {
    if (_version == state.flowsVersion || !state.online) return;
    _version = state.flowsVersion;
    state.flowRuns(widget.flow['id'] as String).then((r) {
      if (mounted) setState(() => (_runs = r, _error = null));
    }).catchError((Object e) {
      if (mounted) setState(() => _error = describeError(e));
    });
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    _load(state);
    final p = context.palette;
    final steps = [for (final s in widget.flow['steps'] as List) Map<String, dynamic>.from(s as Map)];
    return Scaffold(
      appBar: AppBar(title: Text(widget.flow['name'] as String? ?? '記録'), actions: [
        IconButton(
          tooltip: '今すぐ動かす',
          onPressed: () => state.runFlow(widget.flow['id'] as String, widget.flow['name'] as String? ?? ''),
          icon: const Icon(Icons.play_arrow_rounded),
        ),
      ]),
      body: ListView(padding: const EdgeInsets.fromLTRB(14, 6, 14, 24), children: [
        SectionCard(
          title: '流れ',
          subtitle: widget.flow['summary'] as String?,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (var i = 0; i < steps.length; i++)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 3),
                child: Row(children: [
                  CircleAvatar(radius: 11, backgroundColor: p.sunken, child: Text('${i + 1}', style: TextStyle(fontSize: 11, color: p.muted))),
                  const SizedBox(width: 8),
                  Icon(_stepIcons[steps[i]['type']] ?? Icons.circle, size: 17, color: context.colors.primary),
                  const SizedBox(width: 8),
                  Expanded(child: Text((steps[i]['label'] as String?)?.isNotEmpty == true ? steps[i]['label'] as String : steps[i]['type'] as String)),
                ]),
              ),
          ]),
        ),
        const SizedBox(height: 12),
        if (_error != null) Text(_error!, style: TextStyle(color: p.bad)),
        if (_runs == null && _error == null) const Center(child: Padding(padding: EdgeInsets.all(24), child: CircularProgressIndicator())),
        if (_runs != null && _runs!.isEmpty) Text('まだ記録がありません', style: TextStyle(color: p.muted)),
        for (final run in _runs ?? const <Map<String, dynamic>>[]) _RunCard(run: run, first: identical(run, _runs!.first)),
      ]),
    );
  }
}

class _RunCard extends StatelessWidget {
  const _RunCard({required this.run, required this.first});
  final Map<String, dynamic> run;
  final bool first;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final status = _status[run['status']] ?? (run['status'] as String? ?? '', Tone.neutral);
    final log = [for (final s in run['log'] as List? ?? const []) Map<String, dynamic>.from(s as Map)];
    final started = run['started'] as num?, finished = run['finished'] as num?;
    const sources = {'manual': '手動', 'phone': 'スマホから', 'schedule': '時刻', 'interval': '間隔', 'webhook': 'Webhook', 'file': 'ファイル'};
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ExpansionTile(
        initiallyExpanded: first,
        shape: const Border(),
        title: Row(children: [
          Pill(status.$1, tone: status.$2),
          const SizedBox(width: 8),
          Text(_when(started), style: const TextStyle(fontSize: 14)),
          const SizedBox(width: 8),
          Text(sources[run['source']] ?? '${run['source']}', style: TextStyle(color: p.muted, fontSize: 12.5)),
          if (started != null && finished != null) ...[
            const SizedBox(width: 8),
            Text('${(finished - started).toStringAsFixed(1)}秒', style: TextStyle(color: p.muted, fontSize: 12.5)),
          ],
        ]),
        childrenPadding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
        children: [
          for (final s in log)
            Container(
              margin: const EdgeInsets.only(top: 6),
              padding: const EdgeInsets.only(left: 10),
              decoration: BoxDecoration(border: Border(left: BorderSide(color: s['status'] == 'error' ? p.bad : p.line, width: 2))),
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                Row(children: [
                  Icon(_stepIcons[s['type']] ?? Icons.circle, size: 15, color: p.muted),
                  const SizedBox(width: 6),
                  Expanded(child: Text('${s['n']}. ${s['label']}', style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13))),
                  Text('${s['seconds']}秒', style: TextStyle(color: p.muted, fontSize: 12)),
                ]),
                if ((s['output'] as String? ?? '').isNotEmpty)
                  Container(
                    margin: const EdgeInsets.only(top: 4),
                    padding: const EdgeInsets.all(8),
                    decoration: BoxDecoration(color: p.sunken, borderRadius: BorderRadius.circular(8)),
                    child: SelectableText(s['output'] as String, style: const TextStyle(fontSize: 12.5, height: 1.45)),
                  ),
              ]),
            ),
        ],
      ),
    );
  }
}
