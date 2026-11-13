/// Celestial の端末内AI。llama.cpp を FFI で呼び、モデルの実行は専用の isolate で行う(画面を止めない)。
library;

import 'dart:async';
import 'dart:convert';
import 'dart:ffi';
import 'dart:io';
import 'dart:isolate';

import 'package:ffi/ffi.dart';

/// 会話の1発言。role は system / user / assistant。
class LlmMessage {
  const LlmMessage(this.role, this.content);
  final String role;
  final String content;
}

class LlmOptions {
  const LlmOptions({
    this.temperature = 0.7,
    this.topP = 0.9,
    this.minP = 0.05,
    this.repeatPenalty = 1.1,
    this.maxTokens = 1024,
    this.seed,
  });
  final double temperature;
  final double topP;
  final double minP;
  final double repeatPenalty;
  final int maxTokens;
  final int? seed;
}

/// 1回の生成の結果(速さの計測つき)。
class LlmResult {
  LlmResult(this.reason, this.info, this.promptMs, this.generateMs);

  /// eog(自然に終わった)/ stopped(停止した)/ limit(長さの上限)/ error
  final String reason;
  final Map<String, dynamic> info;
  final int promptMs;
  final int generateMs;

  int get generated => (info['n_generated'] as num?)?.toInt() ?? 0;
  int get promptTokens => (info['n_prompt'] as num?)?.toInt() ?? 0;
  int get reusedTokens => (info['n_reused'] as num?)?.toInt() ?? 0;
  double get tokensPerSecond => generateMs <= 0 ? 0 : generated * 1000 / generateMs;
}

class LlmException implements Exception {
  LlmException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// CPU が対応する命令に合わせて、最も速いライブラリを選ぶ。
String libraryForThisDevice() {
  if (!Platform.isAndroid) return 'libcelestial_llm.so';
  try {
    final cpuinfo = File('/proc/cpuinfo').readAsStringSync();
    final features = RegExp(r'^Features\s*:\s*(.*)$', multiLine: true).allMatches(cpuinfo).map((m) => m.group(1)!).join(' ').split(RegExp(r'\s+')).toSet();
    if (features.contains('i8mm') && features.contains('asimddp')) return 'libcelestial_llm_i8mm.so';
    if (features.contains('asimddp')) return 'libcelestial_llm_dotprod.so';
  } catch (_) {
    // 読めなければ、どの端末でも動く版を使う
  }
  return 'libcelestial_llm.so';
}

/// 端末の「速い CPU コア」の数(生成に使うスレッド数の目安)。
int recommendedThreads() {
  final cores = Platform.numberOfProcessors;
  if (cores >= 8) return 4;
  if (cores >= 6) return 4;
  return (cores - 1).clamp(1, 4);
}

typedef _LoadC = Pointer<Void> Function(Pointer<Utf8>, Int32, Int32, Int32, Pointer<Utf8>, Int32);
typedef _LoadD = Pointer<Void> Function(Pointer<Utf8>, int, int, int, Pointer<Utf8>, int);
typedef _StartC = Int32 Function(Pointer<Void>, Pointer<Pointer<Utf8>>, Pointer<Pointer<Utf8>>, Int32, Float, Float, Float, Float, Int32, Uint32, Pointer<Utf8>, Int32);
typedef _StartD = int Function(Pointer<Void>, Pointer<Pointer<Utf8>>, Pointer<Pointer<Utf8>>, int, double, double, double, double, int, int, Pointer<Utf8>, int);
typedef _NextC = Int32 Function(Pointer<Void>, Pointer<Uint8>, Int32);
typedef _NextD = int Function(Pointer<Void>, Pointer<Uint8>, int);
typedef _HandleC = Void Function(Pointer<Void>);
typedef _HandleD = void Function(Pointer<Void>);
typedef _InfoC = Int32 Function(Pointer<Void>, Pointer<Utf8>, Int32);
typedef _InfoD = int Function(Pointer<Void>, Pointer<Utf8>, int);

class _Native {
  _Native(String library) : lib = DynamicLibrary.open(library) {
    load = lib.lookupFunction<_LoadC, _LoadD>('cl_load');
    start = lib.lookupFunction<_StartC, _StartD>('cl_start');
    next = lib.lookupFunction<_NextC, _NextD>('cl_next');
    stop = lib.lookupFunction<_HandleC, _HandleD>('cl_stop');
    reset = lib.lookupFunction<_HandleC, _HandleD>('cl_reset');
    free = lib.lookupFunction<_HandleC, _HandleD>('cl_free');
    info = lib.lookupFunction<_InfoC, _InfoD>('cl_info');
  }
  final DynamicLibrary lib;
  late final _LoadD load;
  late final _StartD start;
  late final _NextD next;
  late final _HandleD stop;
  late final _HandleD reset;
  late final _HandleD free;
  late final _InfoD info;

  Map<String, dynamic> infoOf(Pointer<Void> handle) {
    final buf = calloc<Uint8>(1024);
    try {
      final n = info(handle, buf.cast(), 1024);
      if (n <= 0) return {};
      return jsonDecode(buf.cast<Utf8>().toDartString(length: n)) as Map<String, dynamic>;
    } finally {
      calloc.free(buf);
    }
  }
}

/// 読み込んだ1つのモデル。chat() で回答を少しずつ受け取れる。
class LocalLlm {
  LocalLlm._(this._send, this._events, this._handleAddress, this.library, this.info, this._native);

  final SendPort _send;
  final Stream<dynamic> _events;
  final int _handleAddress;
  final _Native _native; // 停止だけはこの isolate から直接呼ぶ(C 側は原子的な旗を立てるだけ)
  final String library;
  final Map<String, dynamic> info;
  int _nextId = 0;
  bool _busy = false;
  bool _disposed = false;

  bool get busy => _busy;

  static Future<LocalLlm> load(String path, {int contextSize = 4096, int? threads, String? library}) async {
    final lib = library ?? libraryForThisDevice();
    final receive = ReceivePort();
    await Isolate.spawn(_worker, receive.sendPort, debugName: 'celestial-llm');
    final events = receive.asBroadcastStream();
    final send = await events.first as SendPort;
    final t = threads ?? recommendedThreads();
    send.send({'cmd': 'load', 'path': path, 'lib': lib, 'n_ctx': contextSize, 'threads': t, 'threads_batch': Platform.numberOfProcessors.clamp(1, 8)});
    final reply = await events.firstWhere((e) => e is Map && e['type'] == 'loaded') as Map;
    if (reply['error'] != null) {
      receive.close();
      throw LlmException(reply['error'] as String);
    }
    return LocalLlm._(send, events, reply['handle'] as int, lib, Map<String, dynamic>.from(reply['info'] as Map), _Native(lib));
  }

  /// 回答の文字を少しずつ流す。終わると onDone で結果(速さなど)を受け取れる。
  Stream<String> chat(List<LlmMessage> messages, {LlmOptions options = const LlmOptions(), void Function(LlmResult)? onDone}) {
    if (_disposed) throw LlmException('モデルはすでに閉じられています');
    if (_busy) throw LlmException('前の回答を作っている途中です');
    _busy = true;
    final id = ++_nextId;
    final controller = StreamController<String>();
    final sink = const Utf8Decoder(allowMalformed: true).startChunkedConversion(_StringSink(controller.add));
    late StreamSubscription sub;
    sub = _events.listen((event) {
      if (event is! Map || event['id'] != id) return;
      if (event['type'] == 'piece') {
        sink.add((event['bytes'] as TransferableTypedData).materialize().asUint8List());
      } else if (event['type'] == 'done') {
        sink.close();
        _busy = false;
        sub.cancel();
        if (event['error'] != null && event['reason'] == 'error') {
          controller.addError(LlmException(event['error'] as String));
        }
        onDone?.call(LlmResult(event['reason'] as String, Map<String, dynamic>.from(event['info'] as Map? ?? {}),
            event['prompt_ms'] as int? ?? 0, event['generate_ms'] as int? ?? 0));
        controller.close();
      }
    });
    _send.send({
      'cmd': 'chat',
      'id': id,
      'roles': [for (final m in messages) m.role],
      'contents': [for (final m in messages) m.content],
      'temp': options.temperature,
      'top_p': options.topP,
      'min_p': options.minP,
      'repeat': options.repeatPenalty,
      'max_tokens': options.maxTokens,
      'seed': options.seed ?? DateTime.now().microsecondsSinceEpoch & 0x7fffffff,
    });
    return controller.stream;
  }

  /// 生成を止める(1トークン以内に止まる)。
  void stop() {
    if (!_disposed) _native.stop(Pointer<Void>.fromAddress(_handleAddress));
  }

  /// 会話の記憶(KV キャッシュ)を消す。
  void reset() => _send.send({'cmd': 'reset'});

  Future<void> dispose() async {
    if (_disposed) return;
    stop();
    _disposed = true;
    _send.send({'cmd': 'free'});
    await _events.firstWhere((e) => e is Map && e['type'] == 'freed').timeout(const Duration(seconds: 10), onTimeout: () => null);
  }
}

class _StringSink implements Sink<String> {
  _StringSink(this._add);
  final void Function(String) _add;
  @override
  void add(String data) {
    if (data.isNotEmpty) _add(data);
  }

  @override
  void close() {}
}

// ---- 専用の isolate ----

void _worker(SendPort out) {
  final inbox = ReceivePort();
  out.send(inbox.sendPort);
  _Native? native;
  Pointer<Void> handle = nullptr;

  inbox.listen((message) {
    final m = message as Map;
    switch (m['cmd']) {
      case 'load':
        try {
          native = _Native(m['lib'] as String);
          final err = calloc<Uint8>(512);
          final path = (m['path'] as String).toNativeUtf8();
          handle = native!.load(path, m['n_ctx'] as int, m['threads'] as int, m['threads_batch'] as int, err.cast(), 512);
          calloc.free(path);
          if (handle == nullptr) {
            final reason = err.cast<Utf8>().toDartString();
            calloc.free(err);
            out.send({'type': 'loaded', 'error': 'モデルを読み込めませんでした($reason)'});
            return;
          }
          calloc.free(err);
          out.send({'type': 'loaded', 'handle': handle.address, 'info': native!.infoOf(handle)});
        } catch (e) {
          out.send({'type': 'loaded', 'error': 'エンジンを読み込めませんでした($e)'});
        }
      case 'chat':
        _chat(native!, handle, m, out);
      case 'reset':
        if (handle != nullptr) native!.reset(handle);
      case 'free':
        if (handle != nullptr) native!.free(handle);
        handle = nullptr;
        out.send({'type': 'freed'});
        inbox.close();
    }
  });
}

void _chat(_Native native, Pointer<Void> handle, Map m, SendPort out) {
  final id = m['id'];
  final roles = (m['roles'] as List).cast<String>();
  final contents = (m['contents'] as List).cast<String>();
  final n = roles.length;
  final rolePtrs = calloc<Pointer<Utf8>>(n);
  final contentPtrs = calloc<Pointer<Utf8>>(n);
  for (var i = 0; i < n; i++) {
    rolePtrs[i] = roles[i].toNativeUtf8();
    contentPtrs[i] = contents[i].toNativeUtf8();
  }
  final err = calloc<Uint8>(512);
  final buf = calloc<Uint8>(256);
  final watch = Stopwatch()..start();
  var reason = 'eog';
  String? error;
  var promptMs = 0;
  try {
    final rc = native.start(handle, rolePtrs, contentPtrs, n, (m['temp'] as num).toDouble(), (m['top_p'] as num).toDouble(),
        (m['min_p'] as num).toDouble(), (m['repeat'] as num).toDouble(), m['max_tokens'] as int, m['seed'] as int, err.cast(), 512);
    promptMs = watch.elapsedMilliseconds;
    if (rc == -2) {
      reason = 'stopped';
    } else if (rc == -3) {
      reason = 'error';
      error = '会話が長すぎます。新しい会話を始めてください';
    } else if (rc < 0) {
      reason = 'error';
      error = err.cast<Utf8>().toDartString();
    } else {
      final maxTokens = m['max_tokens'] as int;
      var produced = 0;
      while (true) {
        final k = native.next(handle, buf, 256);
        if (k > 0) {
          out.send({'type': 'piece', 'id': id, 'bytes': TransferableTypedData.fromList([buf.asTypedList(k)])});
          produced++;
          continue;
        }
        if (k == -2) {
          reason = 'stopped';
        } else if (k == -1) {
          reason = 'error';
          error = '生成中にエラーが起きました';
        } else {
          final info = native.infoOf(handle);
          reason = ((info['n_generated'] as num?)?.toInt() ?? produced) >= maxTokens ? 'limit' : 'eog';
        }
        break;
      }
    }
  } finally {
    for (var i = 0; i < n; i++) {
      calloc.free(rolePtrs[i]);
      calloc.free(contentPtrs[i]);
    }
    calloc.free(rolePtrs);
    calloc.free(contentPtrs);
    calloc.free(err);
    calloc.free(buf);
  }
  out.send({
    'type': 'done',
    'id': id,
    'reason': reason,
    'error': error,
    'info': native.infoOf(handle),
    'prompt_ms': promptMs,
    'generate_ms': watch.elapsedMilliseconds - promptMs,
  });
}
