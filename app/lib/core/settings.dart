import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';

/// 端末の設定(PC との接続情報、端末内AIの設定、見た目)。JSON ファイルに保存する。
class Settings extends ChangeNotifier {
  Settings(this.file);

  final File file;

  String host = '';
  int port = 8765;
  String token = '';
  String deviceId = '';
  String deviceName = '';
  String pcName = '';
  ThemeMode themeMode = ThemeMode.system;
  String localModel = '';
  int contextSize = 4096;
  int threads = 0; // 0 = 自動
  double temperature = 0.6;
  bool preferPc = true; // PC につながっていれば PC で答える
  bool agent = false;
  int screenWidth = 1280;
  bool notifications = true;
  // PC から同期した「AIへの指示」と「覚えていること」(PC につながっていないときも、スマホの中のAIが使う)
  String instructions = '';
  List<String> memories = [];

  bool get paired => token.isNotEmpty && host.isNotEmpty;

  Future<void> load() async {
    if (!await file.exists()) return;
    try {
      final j = jsonDecode(await file.readAsString()) as Map<String, dynamic>;
      host = j['host'] as String? ?? host;
      port = (j['port'] as num?)?.toInt() ?? port;
      token = j['token'] as String? ?? token;
      deviceId = j['device_id'] as String? ?? deviceId;
      deviceName = j['device_name'] as String? ?? deviceName;
      pcName = j['pc_name'] as String? ?? pcName;
      themeMode = ThemeMode.values.firstWhere((m) => m.name == j['theme'], orElse: () => ThemeMode.system);
      localModel = j['local_model'] as String? ?? localModel;
      contextSize = (j['context_size'] as num?)?.toInt() ?? contextSize;
      threads = (j['threads'] as num?)?.toInt() ?? threads;
      temperature = (j['temperature'] as num?)?.toDouble() ?? temperature;
      preferPc = j['prefer_pc'] as bool? ?? preferPc;
      agent = j['agent'] as bool? ?? agent;
      screenWidth = (j['screen_width'] as num?)?.toInt() ?? screenWidth;
      notifications = j['notifications'] as bool? ?? notifications;
      instructions = j['instructions'] as String? ?? instructions;
      memories = [for (final m in (j['memories'] as List? ?? const [])) if (m is String) m];
    } catch (_) {
      // 読めなければ既定値のまま
    }
  }

  Future<void> save() async {
    final j = {
      'host': host,
      'port': port,
      'token': token,
      'device_id': deviceId,
      'device_name': deviceName,
      'pc_name': pcName,
      'theme': themeMode.name,
      'local_model': localModel,
      'context_size': contextSize,
      'threads': threads,
      'temperature': temperature,
      'prefer_pc': preferPc,
      'agent': agent,
      'screen_width': screenWidth,
      'notifications': notifications,
      'instructions': instructions,
      'memories': memories,
    };
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsString(jsonEncode(j), flush: true);
    await tmp.rename(file.path);
    notifyListeners();
  }

  void update(void Function(Settings s) change) {
    change(this);
    save();
  }
}
