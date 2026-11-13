import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:celestial_llm/celestial_llm.dart';
import 'package:flutter/foundation.dart';

import '../net/pc_client.dart';

/// PC から受け取れるモデル(/api/phone/models の1件)。
class RemoteModel {
  RemoteModel(this.json);
  final Map<String, dynamic> json;
  String get name => json['name'] as String;
  int get fileSize => (json['file_size'] as num).toInt();
  String get family => json['family'] as String? ?? '';
  String get parameters => json['parameters'] as String? ?? '';
  String get quantization => json['quantization'] as String? ?? '';
  double? get accuracy => (json['accuracy'] as num?)?.toDouble();
  bool get recommended => json['recommended'] == true;
  bool get own => json['own'] == true;
}

/// 端末に入っているモデル。
class InstalledModel {
  InstalledModel(this.file, this.meta);
  final File file;
  final Map<String, dynamic> meta;
  String get name => meta['name'] as String? ?? file.uri.pathSegments.last;
  int get size => (meta['file_size'] as num?)?.toInt() ?? file.lengthSync();
  String get description => [meta['family'], meta['parameters'], meta['quantization']].where((e) => e != null && '$e'.isNotEmpty).join(' ');
}

String _fileNameFor(String name) => '${name.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '-')}.gguf';

/// 端末内AI(llama.cpp)の管理。モデルは PC から Tailscale 経由で受け取り、端末の中だけで動かす。
class LocalAi extends ChangeNotifier {
  LocalAi(this.dir);

  final Directory dir;
  final List<InstalledModel> installed = [];
  final Map<String, double> downloads = {};
  final Map<String, String> downloadErrors = {};
  final Set<String> _cancelled = {};
  LocalLlm? _llm;
  String? loadedName;
  bool loading = false;
  String? loadError;
  double lastSpeed = 0;
  int lastPromptMs = 0;
  int lastReused = 0;

  bool get supported => Platform.isAndroid;
  String get variant => libraryForThisDevice();
  bool get busy => _llm?.busy ?? false;

  String get variantLabel => switch (variant) {
        'libcelestial_llm_i8mm.so' => 'ARMv8.6 + i8mm(最速)',
        'libcelestial_llm_dotprod.so' => 'ARMv8.2 + dotprod(高速)',
        _ => '標準(どの端末でも動く版)',
      };

  Future<void> scan() async {
    if (!await dir.exists()) await dir.create(recursive: true);
    installed.clear();
    await for (final entry in dir.list()) {
      if (entry is File && entry.path.endsWith('.gguf')) {
        final metaFile = File('${entry.path}.json');
        Map<String, dynamic> meta = {};
        if (await metaFile.exists()) {
          try {
            meta = jsonDecode(await metaFile.readAsString()) as Map<String, dynamic>;
          } catch (_) {}
        }
        installed.add(InstalledModel(entry, meta));
      }
    }
    installed.sort((a, b) => a.name.compareTo(b.name));
    notifyListeners();
  }

  InstalledModel? find(String name) {
    for (final m in installed) {
      if (m.name == name) return m;
    }
    return null;
  }

  Future<void> download(PcClient client, RemoteModel model) async {
    if (downloads.containsKey(model.name)) return;
    downloads[model.name] = 0;
    downloadErrors.remove(model.name);
    _cancelled.remove(model.name);
    notifyListeners();
    final dest = File('${dir.path}/${_fileNameFor(model.name)}');
    var lastNotify = DateTime.now();
    try {
      await client.download('/api/phone/models/file', {'name': model.name}, dest, cancelled: () => _cancelled.contains(model.name),
          onProgress: (received, total) {
        if (total > 0) downloads[model.name] = received / total;
        if (DateTime.now().difference(lastNotify).inMilliseconds > 200) {
          lastNotify = DateTime.now();
          notifyListeners();
        }
      });
      await File('${dest.path}.json').writeAsString(jsonEncode({...model.json, 'installed': DateTime.now().toIso8601String()}));
    } catch (e) {
      downloadErrors[model.name] = describeError(e);
    } finally {
      downloads.remove(model.name);
      await scan();
    }
  }

  void cancelDownload(String name) {
    _cancelled.add(name);
    notifyListeners();
  }

  Future<void> delete(InstalledModel model) async {
    if (loadedName == model.name) await unload();
    for (final f in [model.file, File('${model.file.path}.json'), File('${model.file.path}.part')]) {
      if (await f.exists()) await f.delete();
    }
    await scan();
  }

  Future<LocalLlm> ensure(String name, {int contextSize = 4096, int threads = 0}) async {
    if (_llm != null && loadedName == name) return _llm!;
    final model = find(name);
    if (model == null) throw LlmException('端末内のAI「$name」が見つかりません。「AIモデル」で入れてください');
    await unload();
    loading = true;
    loadError = null;
    notifyListeners();
    try {
      _llm = await LocalLlm.load(model.file.path, contextSize: contextSize, threads: threads > 0 ? threads : null);
      loadedName = name;
      return _llm!;
    } catch (e) {
      loadError = e.toString();
      rethrow;
    } finally {
      loading = false;
      notifyListeners();
    }
  }

  Stream<String> generate(String name, List<LlmMessage> messages, {required LlmOptions options, int contextSize = 4096, int threads = 0}) async* {
    final llm = await ensure(name, contextSize: contextSize, threads: threads);
    yield* llm.chat(messages, options: options, onDone: (result) {
      lastSpeed = result.tokensPerSecond;
      lastPromptMs = result.promptMs;
      lastReused = result.reusedTokens;
      notifyListeners();
    });
  }

  void stop() => _llm?.stop();

  Future<void> unload() async {
    final llm = _llm;
    _llm = null;
    loadedName = null;
    if (llm != null) await llm.dispose();
    notifyListeners();
  }
}
