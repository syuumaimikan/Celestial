import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

/// PC の Celestial との通信。Tailscale の中だけを通るので HTTP で話す(暗号化は Tailscale が行う)。
class PcClient {
  PcClient({required this.host, required this.port, this.token}) {
    _http.connectionTimeout = const Duration(seconds: 6);
    _http.idleTimeout = const Duration(seconds: 20);
  }

  final String host;
  final int port;
  final String? token;
  final HttpClient _http = HttpClient();

  Uri uri(String path, [Map<String, String>? query]) => Uri(scheme: 'http', host: host, port: port, path: path, queryParameters: query);

  void close() => _http.close(force: true);

  Future<HttpClientRequest> _open(String method, String path, {Map<String, String>? query, Map<String, String>? headers}) async {
    final request = await _http.openUrl(method, uri(path, query));
    if (token != null && token!.isNotEmpty) request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
    headers?.forEach(request.headers.set);
    return request;
  }

  Future<dynamic> _readJson(HttpClientResponse response) async {
    final text = await utf8.decodeStream(response);
    dynamic data;
    try {
      data = text.isEmpty ? {} : jsonDecode(text);
    } catch (_) {
      data = {'error': text};
    }
    if (response.statusCode >= 400) {
      throw PcException(response.statusCode, data is Map && data['error'] != null ? data['error'].toString() : 'エラー(${response.statusCode})');
    }
    return data;
  }

  Future<Map<String, dynamic>> getJson(String path, {Map<String, String>? query, Duration timeout = const Duration(seconds: 20)}) async {
    final request = await _open('GET', path, query: query);
    final response = await request.close().timeout(timeout);
    return Map<String, dynamic>.from(await _readJson(response) as Map);
  }

  Future<HttpClientRequest> _openJson(String path, Object body) async {
    final bytes = utf8.encode(jsonEncode(body));
    final request = await _open('POST', path, headers: {'Content-Type': 'application/json; charset=utf-8'});
    request.contentLength = bytes.length;  // 分割送信(chunked)にしない
    request.add(bytes);
    return request;
  }

  Future<Map<String, dynamic>> postJson(String path, Object body, {Duration timeout = const Duration(seconds: 30)}) async {
    final request = await _openJson(path, body);
    final response = await request.close().timeout(timeout);
    return Map<String, dynamic>.from(await _readJson(response) as Map);
  }

  /// 1行1JSON の流れ(実行の出来事)を受け取る。
  Stream<Map<String, dynamic>> postLines(String path, Object body) async* {
    final request = await _openJson(path, body);
    final response = await request.close();
    if (response.statusCode >= 400) {
      await _readJson(response);
      return;
    }
    yield* response.transform(utf8.decoder).transform(const LineSplitter()).where((l) => l.trim().isNotEmpty).map((l) => Map<String, dynamic>.from(jsonDecode(l) as Map));
  }

  Stream<Map<String, dynamic>> getLines(String path, {Map<String, String>? query}) async* {
    final request = await _open('GET', path, query: query);
    final response = await request.close();
    if (response.statusCode >= 400) {
      await _readJson(response);
      return;
    }
    yield* response.transform(utf8.decoder).transform(const LineSplitter()).where((l) => l.trim().isNotEmpty).map((l) => Map<String, dynamic>.from(jsonDecode(l) as Map));
  }

  /// Server-Sent Events(同期・確認・送られてきた文字などの出来事)。
  Stream<Map<String, dynamic>> events({int? after}) async* {
    final request = await _open('GET', '/api/events', query: after != null ? {'after': '$after'} : null, headers: {'Accept': 'text/event-stream'});
    final response = await request.close();
    if (response.statusCode >= 400) {
      await _readJson(response);
      return;
    }
    await for (final line in response.transform(utf8.decoder).transform(const LineSplitter())) {
      if (line.startsWith('data: ')) {
        try {
          yield Map<String, dynamic>.from(jsonDecode(line.substring(6)) as Map);
        } catch (_) {
          // 壊れた行は飛ばす
        }
      }
    }
  }

  /// PC の画面。変わっていなければ (null, ...) を返す。
  Future<ScreenFrame?> screen({required int width, String? etag}) async {
    final quality = width <= 960 ? 60 : width <= 1280 ? 70 : 82;
    final request = await _open('GET', '/api/remote/screen', query: {'w': '$width', 'f': 'jpeg', 'q': '$quality'}, headers: {'If-None-Match': ?etag});
    final response = await request.close().timeout(const Duration(seconds: 10));
    if (response.statusCode == 304) {
      await response.drain<void>();
      return null;
    }
    if (response.statusCode >= 400) {
      await _readJson(response);
      return null;
    }
    final builder = BytesBuilder(copy: false);
    await for (final chunk in response) {
      builder.add(chunk);
    }
    int header(String name) => int.tryParse(response.headers.value(name) ?? '') ?? 0;
    return ScreenFrame(builder.takeBytes(), response.headers.value('etag'), header('x-screen-width'), header('x-screen-height'),
        header('x-image-width'), header('x-image-height'));
  }

  /// ファイルを受け取る。途中まであれば続きから(Range)。
  Future<void> download(String path, Map<String, String> query, File dest,
      {void Function(int received, int total)? onProgress, bool Function()? cancelled}) async {
    final partial = File('${dest.path}.part');
    var start = await partial.exists() ? await partial.length() : 0;
    final request = await _open('GET', path, query: query, headers: {if (start > 0) 'Range': 'bytes=$start-'});
    final response = await request.close();
    if (response.statusCode == 416) {
      start = 0;
      await partial.delete();
      await response.drain<void>();
      return download(path, query, dest, onProgress: onProgress, cancelled: cancelled);
    }
    if (response.statusCode >= 400) {
      await _readJson(response);
      return;
    }
    if (response.statusCode == 200) start = 0;
    final total = start + (response.contentLength >= 0 ? response.contentLength : 0);
    final sink = partial.openWrite(mode: start > 0 ? FileMode.append : FileMode.write);
    var received = start;
    try {
      await for (final chunk in response) {
        if (cancelled?.call() ?? false) throw const PcException(0, '中止しました');
        sink.add(chunk);
        received += chunk.length;
        onProgress?.call(received, total);
      }
    } finally {
      await sink.close();
    }
    if (total > 0 && received < total) throw const PcException(0, '途中で切れました。もう一度押すと続きから受け取ります');
    if (await dest.exists()) await dest.delete();
    await partial.rename(dest.path);
  }

  Future<Map<String, dynamic>> upload(String remotePath, File file, {void Function(int sent, int total)? onProgress}) async {
    final total = await file.length();
    final request = await _open('PUT', '/api/files/raw', query: {'path': remotePath}, headers: {'Content-Type': 'application/octet-stream'});
    request.contentLength = total;
    var sent = 0;
    await for (final chunk in file.openRead()) {
      request.add(chunk);
      sent += chunk.length;
      onProgress?.call(sent, total);
    }
    final response = await request.close();
    return Map<String, dynamic>.from(await _readJson(response) as Map);
  }
}

class ScreenFrame {
  ScreenFrame(this.bytes, this.etag, this.screenWidth, this.screenHeight, this.imageWidth, this.imageHeight);
  final Uint8List bytes;
  final String? etag;
  final int screenWidth, screenHeight, imageWidth, imageHeight;
}

class PcException implements Exception {
  const PcException(this.status, this.message);
  final int status;
  final String message;
  bool get unauthorized => status == 401;
  @override
  String toString() => message;
}

/// 通信の失敗を、利用者に伝える言葉にする。
String describeError(Object e) {
  if (e is PcException) return e.message;
  if (e is SocketException) return 'PC につながりません。PC の Celestial と Tailscale が動いているか確かめてください';
  if (e is TimeoutException) return 'PC からの応答がありません';
  if (e is HttpException) return '通信が途中で切れました';
  return e.toString();
}
