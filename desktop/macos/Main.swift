import AppKit
import WebKit

struct DesktopPaths {
    let support: URL
    let data: URL
    let server: URL
    let python: URL
    let fingerprint: String
    let port: Int
    var home: URL { URL(string: "http://127.0.0.1:\(port)/studio")! }

    init(bundle: Bundle = .main) throws {
        let fm = FileManager.default
        guard let resources = bundle.resourceURL else { throw Self.invalid("应用资源缺失，请重新安装。") }
        let override = ProcessInfo.processInfo.environment["YUKI_DESKTOP_SUPPORT_DIR"]
        let defaultSupport = try fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("YukiStation", isDirectory: true)
        support = override.map { URL(fileURLWithPath: $0, isDirectory: true) } ?? defaultSupport
        let settings = support.appendingPathComponent("desktop-local.json")
        var configuration: [String: Any] = [:]
        if fm.fileExists(atPath: settings.path) {
            guard let parsed = try JSONSerialization.jsonObject(with: Data(contentsOf: settings)) as? [String: Any], parsed["schema"] as? String == "yuki-desktop-local/v1", Set(parsed.keys).isSubset(of: ["schema", "dataDirectory", "port"]) else { throw Self.invalid("本机桌面配置无效；未创建新数据目录。") }
            configuration = parsed
        }
        if configuration["dataDirectory"] != nil {
            guard let raw = configuration["dataDirectory"] as? String else { throw Self.invalid("既有数据目录配置类型无效；未创建新数据目录。") }
            guard raw.hasPrefix("/") else { throw Self.invalid("既有数据目录须为绝对路径。") }
            data = URL(fileURLWithPath: raw, isDirectory: true).resolvingSymlinksInPath()
            guard fm.fileExists(atPath: data.path) else { throw Self.invalid("配置中的既有数据目录不存在；未改为新建空工作区。") }
        } else { data = support.appendingPathComponent("data", isDirectory: true) }
        if configuration["port"] != nil && !(configuration["port"] is Int) { throw Self.invalid("本机端口配置类型无效。") }
        let selected = configuration["port"] as? Int ?? 8767
        guard (1024...65535).contains(selected) else { throw Self.invalid("本机端口须为1024至65535。") }
        port = selected
        server = resources.appendingPathComponent("server/lancer-local/app.py")
        python = resources.appendingPathComponent("runtime/bin/python3.12")
        guard fm.fileExists(atPath: server.path), fm.isExecutableFile(atPath: python.path) else { throw Self.invalid("应用内置服务或运行时缺失；请重新安装完整应用。") }
        let manifest = try JSONSerialization.jsonObject(with: Data(contentsOf: resources.appendingPathComponent("desktop-manifest.json"))) as? [String: Any]
        guard let expected = manifest?["runtimeFingerprint"] as? String, expected.count == 64 else { throw Self.invalid("应用源码清单缺失。") }
        fingerprint = expected
    }

    static func invalid(_ message: String) -> NSError { NSError(domain: "YukiDesktop", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }

    func validateService(_ status: [String: Any]) throws {
        guard status["schema"] as? String == "yuki-desktop-service/v1",
              let reportedData = status["dataDir"] as? String,
              URL(fileURLWithPath: reportedData).resolvingSymlinksInPath().standardizedFileURL == data.resolvingSymlinksInPath().standardizedFileURL else {
            throw Self.invalid("本机端口被其他服务或其他数据目录占用。未连接该服务、未新建空工作区。可在本机desktop-local.json指定另一个端口。")
        }
        let build = status["runtimeBuild"] as? [String: Any]
        guard build?["fingerprint"] as? String == fingerprint, status["sourceMatchesRuntimeBuild"] as? Bool == true else {
            throw Self.invalid("后台仍运行其他版本的雪站。为保护进行中的任务，本应用不会自动终止它。请先完成或停止任务，再重启电脑或由维护者更新后台服务。")
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var paths: DesktopPaths!
    var homeURL: URL { paths.home }
    var window: NSWindow!
    var webView: WKWebView!
    var serverProcess: Process?
    var downloads: [ObjectIdentifier: (temporary: URL, destination: URL)] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenus()
        webView = WKWebView(frame: .zero)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "yuki雪站"
        window.minSize = NSSize(width: 1060, height: 680)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        do { paths = try DesktopPaths(); probeService(mayLaunch: true, attempt: 0) }
        catch { showError("雪站安装配置未就绪。", detail: error.localizedDescription) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func buildMenus() {
        let main = NSMenu()
        let app = NSMenu(title: "yuki雪站")
        app.addItem(withTitle: "关于 yuki雪站", action: #selector(showAbout), keyEquivalent: "")
        app.addItem(.separator())
        let serviceNote = NSMenuItem(title: "退出窗口后，后台任务继续运行", action: nil, keyEquivalent: "")
        serviceNote.isEnabled = false
        app.addItem(serviceNote)
        app.addItem(withTitle: "退出 yuki雪站", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        addMenu(app, to: main)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        addMenu(edit, to: main)
        let view = NSMenu(title: "查看")
        view.addItem(withTitle: "刷新", action: #selector(reloadPage), keyEquivalent: "r")
        view.addItem(withTitle: "返回工作台", action: #selector(openHome), keyEquivalent: "1")
        view.addItem(withTitle: "在浏览器中打开", action: #selector(openInBrowser), keyEquivalent: "o")
        addMenu(view, to: main)
        let windows = NSMenu(title: "窗口")
        windows.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        addMenu(windows, to: main)
        NSApp.mainMenu = main
        NSApp.windowsMenu = windows
    }

    func addMenu(_ menu: NSMenu, to main: NSMenu) {
        let item = NSMenuItem()
        item.submenu = menu
        main.addItem(item)
        for child in menu.items where child.action == #selector(showAbout)
            || child.action == #selector(reloadPage) || child.action == #selector(openHome)
            || child.action == #selector(openInBrowser) { child.target = self }
    }

    @objc func showAbout() {
        let alert = NSAlert()
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "未知"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "未知"
        alert.messageText = "yuki雪站 \(version)（\(build)）"
        alert.informativeText = "独立桌面版，内置Python与工作台服务。\n项目数据保存在本机；模型与仓库由每位用户自行连接。关闭窗口后后台服务继续运行。"
        alert.beginSheetModal(for: window)
    }

    @objc func reloadPage() { webView.reload() }
    @objc func openHome() { if paths != nil { webView.load(URLRequest(url: homeURL)) } }
    @objc func openInBrowser() { if paths != nil { NSWorkspace.shared.open(webView.url ?? homeURL) } }

    func probeService(mayLaunch: Bool, attempt: Int) {
        let health = URL(string: "http://127.0.0.1:\(paths.port)/api/desktop/status")!
        let request = URLRequest(url: health, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 2)
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                if let response = response as? HTTPURLResponse {
                    let status = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                    guard response.statusCode == 200, let status = status else {
                        self.showError("本机端口不是此版本雪站。", detail: "未连接该服务，也未启动另一个数据工作区。")
                        return
                    }
                    do { try self.paths.validateService(status) }
                    catch {
                        self.showError("后台身份或版本不匹配。", detail: error.localizedDescription)
                        return
                    }
                    self.openHome()
                    return
                }
                if mayLaunch {
                    guard (error as? URLError)?.code == .cannotConnectToHost else {
                        self.showError("暂时无法确认本地服务状态。", detail: error?.localizedDescription ?? "请稍后重新打开。")
                        return
                    }
                    do { try self.startServer() }
                    catch {
                        self.showError("本地服务未能启动。", detail: error.localizedDescription)
                        return
                    }
                }
                if let process = self.serverProcess, !process.isRunning {
                    self.showError("本地服务启动后退出了。", detail: "退出状态：\(process.terminationStatus)。可用项目中的 start.command 查看启动信息。")
                    return
                }
                guard attempt < 24 else {
                    self.showError("本地服务仍未就绪。", detail: "后台进程已保留；可稍后重新打开窗口，或通过 start.command 查看服务。")
                    return
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                    self.probeService(mayLaunch: false, attempt: attempt + 1)
                }
            }
        }.resume()
    }

    func startServer() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: paths.support, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let logURL = paths.support.appendingPathComponent("service.log")
        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        let logs = try FileHandle(forWritingTo: logURL)
        try logs.seekToEnd()
        let process = Process()
        process.executableURL = paths.python
        process.arguments = ["-E", "-s", "-B", paths.server.path, "--port", String(paths.port), "--data-dir", paths.data.path]
        process.currentDirectoryURL = paths.server.deletingLastPathComponent()
        var environment = ProcessInfo.processInfo.environment
        for key in Array(environment.keys) where key.hasPrefix("PYTHON") { environment.removeValue(forKey: key) }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = logs
        process.standardError = logs
        try process.run()
        // The service owns long-running tasks and deliberately outlives this window.
        serverProcess = process
    }

    func showError(_ message: String, detail: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = detail
        alert.alertStyle = .warning
        alert.addButton(withTitle: "关闭")
        alert.beginSheetModal(for: window)
    }

    func isLocal(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == paths?.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
        } else if let url = navigationAction.request.url,
                  navigationAction.targetFrame?.isMainFrame != false,
                  !isLocal(url), ["http", "https"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let response = navigationResponse.response as? HTTPURLResponse
        let attachment = response?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased().contains("attachment") ?? false
        decisionHandler(attachment || !navigationResponse.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) { webView.load(navigationAction.request) }
            else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = !parameters.allowsDirectories
        panel.beginSheetModal(for: window) { result in
            completionHandler(result == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { result in
            guard result == .OK, let destination = panel.url else {
                completionHandler(nil)
                return
            }
            // WebKit requires a new file; keep an existing export until the new download completes.
            let temporary = destination.deletingLastPathComponent().appendingPathComponent(".yuki-download-" + UUID().uuidString)
            self.downloads[ObjectIdentifier(download)] = (temporary, destination)
            completionHandler(temporary)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let file = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do {
            if FileManager.default.fileExists(atPath: file.destination.path) {
                _ = try FileManager.default.replaceItemAt(file.destination, withItemAt: file.temporary)
            } else {
                try FileManager.default.moveItem(at: file.temporary, to: file.destination)
            }
            NSWorkspace.shared.activateFileViewerSelecting([file.destination])
        } catch {
            try? FileManager.default.removeItem(at: file.temporary)
            showError("导出文件未能保存。", detail: error.localizedDescription)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let file = downloads.removeValue(forKey: ObjectIdentifier(download)) {
            try? FileManager.default.removeItem(at: file.temporary)
        }
        if (error as NSError).code != NSURLErrorCancelled {
            showError("下载未完成。", detail: error.localizedDescription)
        }
    }
}

@main
enum YukiMain {
    @MainActor static func main() {
        if CommandLine.arguments.contains("--check-installation") {
            do {
                let paths = try DesktopPaths()
                if let index = CommandLine.arguments.firstIndex(of: "--health-file"), index + 1 < CommandLine.arguments.count {
                    guard let status = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[index + 1]))) as? [String: Any] else { throw DesktopPaths.invalid("服务状态格式无效。") }
                    try paths.validateService(status)
                }
                let result: [String: Any] = ["dataDirectory": paths.data.path, "supportDirectory": paths.support.path,
                    "serverScript": paths.server.path, "python": paths.python.path,
                    "port": paths.port, "runtimeFingerprint": paths.fingerprint]
                let raw = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
                print(String(decoding: raw, as: UTF8.self))
            } catch { fputs("安装检查失败：\(error.localizedDescription)\n", stderr); exit(1) }
            return
        }
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.run()
    }
}
