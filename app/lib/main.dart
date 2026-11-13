import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'app_state.dart';
import 'theme.dart';
import 'ui/chat_page.dart';
import 'ui/common.dart';
import 'ui/files_page.dart';
import 'ui/flows_page.dart';
import 'ui/models_page.dart';
import 'ui/remote_page.dart';
import 'ui/settings_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final state = AppState();
  runApp(AppScope(state: state, child: const CelestialApp()));
  await state.init();
}

const _japanese = Locale('ja', 'JP');

class CelestialApp extends StatelessWidget {
  const CelestialApp({super.key});

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    if (!state.ready) {
      return MaterialApp(
        debugShowCheckedModeBanner: false,
        locale: _japanese,
        supportedLocales: const [_japanese],
        localizationsDelegates: GlobalMaterialLocalizations.delegates,
        theme: buildTheme(Brightness.light),
        darkTheme: buildTheme(Brightness.dark),
        home: const _Splash(),
      );
    }
    return ListenableBuilder(
      listenable: state.settings,
      builder: (context, _) => MaterialApp(
        title: 'Celestial',
        debugShowCheckedModeBanner: false,
        // 日本語として表示する: 漢字を日本語の字形で描き、コピーや貼り付けなどの標準の表示も日本語にする
        locale: _japanese,
        supportedLocales: const [_japanese],
        localizationsDelegates: GlobalMaterialLocalizations.delegates,
        themeMode: state.settings.themeMode,
        theme: buildTheme(Brightness.light),
        darkTheme: buildTheme(Brightness.dark),
        home: const HomeShell(),
      ),
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();

  @override
  Widget build(BuildContext context) => const Scaffold(body: Center(child: CelestialMark(size: 56)));
}

class _Destination {
  const _Destination(this.label, this.icon, this.selectedIcon);
  final String label;
  final IconData icon;
  final IconData selectedIcon;
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;
  StreamSubscription? _toasts;
  StreamSubscription? _launches;
  final _prefill = ValueNotifier<String?>(null);

  @override
  void initState() {
    super.initState();
    final state = AppScope.read(context);
    _toasts = state.messages.listen((text) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(SnackBar(content: Text(text), duration: const Duration(seconds: 3)));
    });
    _launches = state.launches.listen((_) => _consumeLaunch());
    WidgetsBinding.instance.addPostFrameCallback((_) => _consumeLaunch());
  }

  void _consumeLaunch() {
    if (!mounted) return;
    final data = AppScope.read(context).takeLaunch();
    if (data == null) return;
    handleLaunch(context, data, (text) {
      setState(() => _index = 0);
      _prefill.value = text;
    });
  }

  @override
  void dispose() {
    _toasts?.cancel();
    _launches?.cancel();
    _prefill.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = AppScope.of(context);
    final desktop = state.isDesktop;
    final destinations = desktop
        ? const [
            _Destination('会話', Icons.forum_outlined, Icons.forum),
            _Destination('自動化', Icons.account_tree_outlined, Icons.account_tree),
            _Destination('設定', Icons.tune_outlined, Icons.tune),
          ]
        : const [
            _Destination('会話', Icons.forum_outlined, Icons.forum),
            _Destination('PC操作', Icons.desktop_windows_outlined, Icons.desktop_windows),
            _Destination('ファイル', Icons.folder_outlined, Icons.folder),
            _Destination('AIモデル', Icons.memory_outlined, Icons.memory),
            _Destination('設定', Icons.tune_outlined, Icons.tune),
          ];
    if (_index >= destinations.length) _index = 0;
    final pages = desktop
        ? [ChatPage(prefill: _prefill), const FlowsPage(), const SettingsPage()]
        : [
            ChatPage(prefill: _prefill),
            RemotePage(active: _index == 1),
            FilesPage(active: _index == 2),
            ModelsPage(active: _index == 3),
            const SettingsPage(),
          ];
    final body = IndexedStack(index: _index, children: pages);
    final wide = MediaQuery.sizeOf(context).width >= 840;
    if (wide) {
      return Scaffold(
        body: Row(children: [
          NavigationRail(
            selectedIndex: _index,
            onDestinationSelected: (i) => setState(() => _index = i),
            labelType: NavigationRailLabelType.all,
            leading: const Padding(padding: EdgeInsets.symmetric(vertical: 16), child: CelestialMark(size: 30)),
            destinations: [
              for (final d in destinations) NavigationRailDestination(icon: Icon(d.icon), selectedIcon: Icon(d.selectedIcon), label: Text(d.label)),
            ],
          ),
          Expanded(child: body),
        ]),
      );
    }
    return Scaffold(
      body: body,
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: [
          for (final d in destinations) NavigationDestination(icon: Icon(d.icon), selectedIcon: Icon(d.selectedIcon), label: d.label),
        ],
      ),
    );
  }
}
