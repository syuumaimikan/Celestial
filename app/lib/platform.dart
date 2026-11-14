import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'core/models.dart';

/// アプリの版(pubspec.yaml の version と同じにする。Android 以外で使う)。
const kAppVersion = '0.3.0';

class PickedFile {
  PickedFile(this.path, this.name, this.size);
  final String path;
  final String name;
  final int size;
}

/// Android 固有の機能(QR から開くリンク、共有、ファイルの選択と保存、通知など)への橋渡し。
/// Windows では使えないものは何もしない。
class PlatformBridge {
  static const _channel = MethodChannel('dev.celestial/platform');
  static const _intents = EventChannel('dev.celestial/intents');

  static bool get _android => debugIsAndroid ?? Platform.isAndroid;

  /// 試験で Android として動かすため(通常は null)。
  @visibleForTesting
  static bool? debugIsAndroid;

  /// 起動のきっかけになったリンク(celestial://pair?...)や共有された文字。
  static Future<Map<String, dynamic>> launchData() async {
    if (!_android) return {};
    final data = await _channel.invokeMapMethod<String, dynamic>('launchData');
    return data ?? {};
  }

  /// 起動中に届いたリンクや共有。
  static Stream<Map<String, dynamic>> intents() {
    if (!_android) return const Stream.empty();
    return _intents.receiveBroadcastStream().map((e) => Map<String, dynamic>.from(e as Map));
  }

  static Future<String> deviceName() async {
    if (!_android) return Platform.localHostname;
    return await _channel.invokeMethod<String>('deviceName') ?? 'Android';
  }

  static Future<PickedFile?> pickFile() async {
    if (Platform.isWindows) return _pickFileWindows();
    if (!_android) return null;
    final r = await _channel.invokeMapMethod<String, dynamic>('pickFile');
    if (r == null) return null;
    return PickedFile(r['path'] as String, r['name'] as String, (r['size'] as num).toInt());
  }

  /// Windows の「ファイルを開く」画面で選ぶ(追加の部品なしで使えるように、PowerShell 経由で出す)。
  static Future<PickedFile?> _pickFileWindows() async {
    const script = r'Add-Type -AssemblyName System.Windows.Forms; '
        r'$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Title = "添付するファイル"; '
        r'if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $d.FileName }';
    final r = await Process.run('powershell', ['-NoProfile', '-STA', '-Command', script], stdoutEncoding: const Utf8Codec());
    final path = (r.stdout as String).trim();
    if (r.exitCode != 0 || path.isEmpty) return null;
    final file = File(path);
    if (!await file.exists()) return null;
    return PickedFile(path, path.split(RegExp(r'[\\/]')).last, await file.length());
  }

  /// 端末の「ダウンロード」フォルダへ保存する。保存先の表示名を返す。
  static Future<String?> saveToDownloads(String path, String name) async {
    if (Platform.isWindows) {
      // Windows では利用者の「ダウンロード」フォルダへ写す(同じ名前があれば番号を付ける)
      final home = Platform.environment['USERPROFILE'];
      if (home == null) return path;
      final dir = Directory('$home\\Downloads');
      await dir.create(recursive: true);
      final dot = name.lastIndexOf('.');
      final stem = dot > 0 ? name.substring(0, dot) : name, ext = dot > 0 ? name.substring(dot) : '';
      var dest = File('${dir.path}\\$name');
      for (var n = 2; await dest.exists(); n++) {
        dest = File('${dir.path}\\$stem ($n)$ext');
      }
      await File(path).copy(dest.path);
      return dest.path;
    }
    if (!_android) return path;
    return _channel.invokeMethod<String>('saveToDownloads', {'path': path, 'name': name});
  }

  static Future<void> notify(String title, String body) async {
    if (!_android) return;
    await _channel.invokeMethod('notify', {'title': title, 'body': body});
  }

  static Future<void> openUrl(String url) async {
    if (_android) {
      await _channel.invokeMethod('openUrl', {'url': url});
    } else if (Platform.isWindows) {
      await Process.start('explorer', [url]);
    }
  }

  /// ほかのアプリへ文字を共有する(Android の共有メニュー)。
  static Future<void> shareText(String text) async {
    if (!_android) return;
    await _channel.invokeMethod('shareText', {'text': text});
  }

  /// 読み上げ中のメッセージの ID(読み上げていなければ null)。読み終わると Android 側の知らせで null に戻る。
  static final speakingId = ValueNotifier<String?>(null);
  static int _speechToken = 0;
  static bool _listening = false;

  static void _listen() {
    if (_listening) return;
    _listening = true;
    _channel.setMethodCallHandler((call) async {
      final args = Map<String, dynamic>.from(call.arguments as Map);
      switch (call.method) {
        case 'speaking':
          // 前の読み上げの知らせ(止めた直後に別のメッセージを読み始めた場合など)は無視する
          if (args['token'] == _speechToken && args['on'] != true) speakingId.value = null;
        case 'speech':
          final type = args['type'] as String? ?? '';
          if (type == 'final' || type == 'error' || type == 'end') voiceListening.value = false;
          _voice.add((type, args['text'] as String? ?? ''));
      }
    });
  }

  // ---- 音声入力 ----

  static final _voice = StreamController<(String, String)>.broadcast();

  /// 音声入力の出来事 (種類, 文字)。種類は ready / partial(途中の文字)/ final(確定)/ error(説明)/ end。
  static Stream<(String, String)> get voiceEvents => _voice.stream;

  /// 音声入力で聞いている最中か。
  static final voiceListening = ValueNotifier<bool>(false);

  static bool get voiceSupported => _android;

  /// 音声入力を始める(初回はマイクの許可を求める)。文字は voiceEvents に届く。
  static Future<void> startVoice({String lang = 'ja-JP'}) async {
    if (!_android) return;
    _listen();
    voiceListening.value = true;
    try {
      await _channel.invokeMethod('listen', {'lang': lang});
    } catch (_) {
      voiceListening.value = false;
      rethrow;
    }
  }

  /// 話し終わったことにして、確定した文字を受け取る。
  static Future<void> stopVoice() async {
    if (!_android) return;
    await _channel.invokeMethod('stopListening');
  }

  /// 文章を読み上げる(Markdown の記号やコードは読まない)。id は読み上げ中の表示に使う。
  static Future<void> speak(String markdown, {String? id}) async {
    if (!_android) return;
    _listen();
    final token = ++_speechToken;
    speakingId.value = id ?? '';
    try {
      await _channel.invokeMethod('speak', {'text': speakable(markdown), 'token': token});
    } catch (_) {
      if (token == _speechToken) speakingId.value = null;
      rethrow;
    }
  }

  static Future<void> stopSpeaking() async {
    if (!_android) return;
    ++_speechToken;
    speakingId.value = null;
    await _channel.invokeMethod('stopSpeaking');
  }

  /// 読み上げ用に、Markdown の記号を取り除き、コードの部分は省く。
  /// 表は区切りの行を除き、1行ずつ「東京、1,200,000。」のように区切って読む。掛け算の「17 * 23」は「17 × 23」にする。
  static String speakable(String markdown) {
    final body = markdown
        .replaceAll(RegExp(r'\n\n\[添付ファイル: ([^\]\n]+)\]\n````\n[\s\S]*?\n````'), '')
        .replaceAll(RegExp(r'```[\s\S]*?(```|$)'), '(コードは省略します)\n');
    final row = RegExp(r'^\s*\|.*\|\s*$');
    final lines = pipeTables(body)
        .split('\n')
        .where((l) => !(l.contains('|') && tableDelimiter.hasMatch(l))) // 表の区切りの行は読まない
        .map((l) => row.hasMatch(l) ? '${tableCells(l).where((c) => c.isNotEmpty).join('、')}。' : l);
    return lines
        .join('\n')
        .replaceAllMapped(RegExp(r'(\d)\s*\*\s*(\d)'), (m) => '${m[1]} × ${m[2]}')
        .replaceAllMapped(RegExp(r'`([^`]*)`'), (m) => m[1]!)
        .replaceAllMapped(RegExp(r'\[([^\]]*)\]\([^)]*\)'), (m) => m[1]!)
        .replaceAll(RegExp(r'^\s*([-*_])(\s*\1){2,}\s*$', multiLine: true), '') // 区切りの線(---)
        .replaceAll(RegExp(r'^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+', multiLine: true), '')
        .replaceAll(RegExp(r'[*_~|]'), '')
        .replaceAll(RegExp(r'\n{3,}'), '\n\n')
        .trim();
  }

  static Future<void> keepScreenOn(bool on) async {
    if (!_android) return;
    await _channel.invokeMethod('keepScreenOn', {'on': on});
  }

  /// この端末に入っているアプリの版 {name, code}(code は端末の種類ごとに 1000×種類 が足されていることがある)。
  static Future<Map<String, dynamic>> appVersion() async {
    if (!_android) return {'name': kAppVersion, 'code': 0};
    return await _channel.invokeMapMethod<String, dynamic>('appVersion') ?? {'name': kAppVersion, 'code': 0};
  }

  /// 受け取った APK を入れ替える。入れる許可がまだ無ければ設定の画面を開いて 'permission' を返す。
  static Future<String> installApk(String path) async {
    if (!_android) return 'unsupported';
    return await _channel.invokeMethod<String>('installApk', {'path': path}) ?? '';
  }

  static Future<Map<String, dynamic>> deviceInfo() async {
    if (!_android) return {};
    return await _channel.invokeMapMethod<String, dynamic>('deviceInfo') ?? {};
  }
}
