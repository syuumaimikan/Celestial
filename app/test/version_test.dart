import 'dart:io';

import 'package:celestial/platform.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('アプリの版は pubspec.yaml と同じ(更新の確かめに使う)', () {
    final pubspec = File('pubspec.yaml').readAsStringSync();
    final found = RegExp(r'^version:\s*([\w.\-]+)\+(\d+)', multiLine: true).firstMatch(pubspec);
    expect(found, isNotNull);
    expect(kAppVersion, found!.group(1));
  });
}
