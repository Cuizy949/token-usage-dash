import Cocoa
import CoreServices
import ServiceManagement
import Network

// MARK: - 数据模型（对应 ai_usage_fetch.py 的 JSON）

struct ToolUsage: Decodable {
    var name: String?
    var turns: String?
    var tokens: String?
    var model: String?
    var quota_left: Int?
    var reset: String?
    var quota_snapshot_ts: String?
    // ZCode 专属：5 小时窗余量（周窗复用 quota_left/reset）
    var quota_5h_left: Int?
    var reset_5h: String?

    init() {}

    /// 增量合并工具卡：delta 中非 nil 字段覆盖旧值（单工具/全量刷新共用）
    mutating func merge(from delta: ToolUsage) {
        name = delta.name ?? name
        turns = delta.turns ?? turns
        tokens = delta.tokens ?? tokens
        model = delta.model ?? model
        if let q = delta.quota_left { quota_left = q }
        if let q5 = delta.quota_5h_left { quota_5h_left = q5 }
        if let r5 = delta.reset_5h { reset_5h = r5 }
        // reset 非 nil 表示"额度已更新"（全量）：同步应用快照标记（含清除）。
        // WorkBuddy 类无重置时间（reset 恒缺键）但有快照额度：quota_snapshot_ts
        // 非 nil 时同样视为额度已更新，否则快照标记被静默丢弃（2026-09-26 实测：
        // WorkBuddy 卡显示"重置 --"而非"快照 MM-dd HH:MM"）
        if let r = delta.reset {
            reset = r
            quota_snapshot_ts = delta.quota_snapshot_ts
        } else if delta.quota_snapshot_ts != nil {
            quota_snapshot_ts = delta.quota_snapshot_ts
        }
    }
}

struct DashData: Decodable {
    var codex: ToolUsage?
    var claude: ToolUsage?
    var hermes: ToolUsage?
    var trae: ToolUsage?
    var zcode: ToolUsage?
    var workbuddy: ToolUsage?
    var total: String?
    var time: String?
    var ok: Bool
    var error: String?
    var fetch_ts: Int?

    static func failure(_ reason: String) -> DashData {
        DashData(codex: nil, claude: nil, hermes: nil, trae: nil, zcode: nil, workbuddy: nil,
                 total: nil, time: nil,
                 ok: false, error: reason, fetch_ts: nil)
    }

    /// 增量合并：delta 中提供的字段覆盖旧值，未提供的保留。
    /// 两模式共用：全量替换语义一致（delta 全字段有值，含三家额度）；单工具只动对应卡。
    /// total 始终按当前值重算。
    func merged(with delta: DashData) -> DashData {
        var base = self
        base.time = delta.time ?? base.time
        base.ok = delta.ok
        if let e = delta.error { base.error = e }
        base.fetch_ts = delta.fetch_ts ?? base.fetch_ts
        if let c = delta.codex {
            if base.codex == nil { base.codex = ToolUsage() }
            base.codex!.merge(from: c)
        }
        if let c = delta.claude {
            if base.claude == nil { base.claude = ToolUsage() }
            base.claude!.merge(from: c)
        }
        if let h = delta.hermes {
            if base.hermes == nil { base.hermes = ToolUsage() }
            base.hermes!.merge(from: h)
        }
        if let t = delta.trae {
            if base.trae == nil { base.trae = ToolUsage() }
            base.trae!.merge(from: t)
        }
        if let z = delta.zcode {
            if base.zcode == nil { base.zcode = ToolUsage() }
            base.zcode!.merge(from: z)
        }
        if let w = delta.workbuddy {
            if base.workbuddy == nil { base.workbuddy = ToolUsage() }
            base.workbuddy!.merge(from: w)
        }
        base.total = DashData.recalcTotal(base)
        return base
    }

    /// 六工具 tokens（parseYi 亿值）求和 → 合计字符串；全空返回 nil
    /// 注意：parseYi 返回亿单位，阈值必须用亿值（≥0.1 亿=1000万 显示亿，≥1e-4 亿=1万 显示万）
    static func recalcTotal(_ d: DashData) -> String? {
        var yi: Double = 0
        var any = false
        for u in [d.codex, d.claude, d.hermes, d.trae, d.zcode, d.workbuddy] {
            guard let t = u?.tokens, t != "--", !t.isEmpty else { continue }
            yi += parseYi(t)
            any = true
        }
        guard any else { return nil }
        if yi >= 0.1 { return String(format: "%.2f亿", yi) }          // ≥1000万 → 亿
        if yi >= 1e-4 { return String(format: "%.1f万", yi * 1e4) }   // ≥1万 → 万
        return String(Int(yi * 1e8))                                  // 原始
    }
}

// MARK: - 布局定位（仓库裸跑与 .app bundle 双布局）
enum BundleLayout {
    static var execDir: String {
        Bundle.main.executableURL?.deletingLastPathComponent().path ?? ""
    }
    /// 依次探测，返回第一个存在的路径（都不存在时返回末选，保持旧行为）
    static func firstExisting(_ cands: [String]) -> String {
        let fm = FileManager.default
        for c in cands where fm.fileExists(atPath: c) { return c }
        return cands.last ?? ""
    }
    /// Python 层与图标资源：仓库布局与本二进制同级（dash/../collectors），
    /// bundle 布局在 Contents/Resources/（codesign 要求 Contents/ 根不放散文件）
    static var fetchScript: String {
        if let p = ProcessInfo.processInfo.environment["AIUSD_FETCH_SCRIPT"] { return p }
        return firstExisting([execDir + "/ai_usage_fetch.py",
                              execDir + "/../Resources/ai_usage_fetch.py"])
    }
    static var collectorsDir: String {
        if let p = ProcessInfo.processInfo.environment["AIUSD_COLLECTORS_DIR"] { return p }
        return firstExisting([execDir + "/../collectors",
                              execDir + "/../Resources/collectors"])
    }
    static var assetsIconsDir: String {
        if let p = ProcessInfo.processInfo.environment["AIUSD_ASSETS_DIR"] { return p }
        return firstExisting([execDir + "/../assets/icons",
                              execDir + "/../Resources/assets/icons"])
    }
    /// 内嵌 Python 运行时（python-build-standalone，自包含 bundle 用）；
    /// 仓库裸跑没有 runtime → 返回 /usr/bin/env shim（走 PATH 里的 python3）。
    /// 环境变量 AIUSD_PYTHON 仍最高优先（见 DataFetcher.venvPython）。
    static var pythonBin: String {
        firstExisting([execDir + "/../Resources/python-runtime/bin/python3",
                       execDir + "/../python-runtime/bin/python3",
                       "/usr/bin/env"])
    }
}

// MARK: - 数据采集（复用 keydash 采集脚本，后台线程执行）

final class DataFetcher {
    static let shared = DataFetcher()

    // 解析器：env AIUSD_PYTHON 最高优先 → bundle 内嵌 runtime → /usr/bin/env shim。
    // 脚本/图标定位统一走 BundleLayout（仓库裸跑与 .app 双布局）。
    var venvPython: String {
        if let p = ProcessInfo.processInfo.environment["AIUSD_PYTHON"] { return p }
        return BundleLayout.pythonBin
    }
    var usesEnvShim: Bool {
        ProcessInfo.processInfo.environment["AIUSD_PYTHON"] == nil
            && BundleLayout.pythonBin == "/usr/bin/env"
    }
    // ReportRunner 复用同一解释器解析
    var pythonPath: String { venvPython }
    private var fetchScript: String { BundleLayout.fetchScript }
    private let fetchTimeout: TimeInterval = 300   // 覆盖 ai_usage_summary 最长 240s
    // 串行采集队列 + 按模式合并：修掉旧 running Bool 的两处问题——
    // ① 主线程(定时器/菜单)与 fs monitor queue 并发读写 Bool 的数据竞争；
    // ② 事件高频时统一 tick 被静默吞掉（额度唯一通道饿死）。排队不丢，同模式合并。
    private let fetchQueue = DispatchQueue(label: "ai_usage_dash.fetch", qos: .utility)
    private var queuedModes = Set<String>()   // 仅主线程读写

    /// 全量采集：Token 统计 + 三家额度（统一更新频率定时器/首次启动/菜单立即刷新）
    func fetch(completion: @escaping (DashData) -> Void) {
        enqueue(mode: "full", args: [], completion: completion)
    }

    /// 全量但不带额度（事件驱动多工具变化用）：额度 GET 严格按用户设置的频率出网，
    /// 不随文件事件提前
    func fetchStatsOnly(completion: @escaping (DashData) -> Void) {
        enqueue(mode: "full-stats", args: ["--no-quota"], completion: completion)
    }

    /// 单工具日志刷新（FSEvents 事件驱动，不携带额度）
    func fetch(tool: String, completion: @escaping (DashData) -> Void) {
        enqueue(mode: "tool:" + tool, args: ["--tool", tool], completion: completion)
    }

    /// 入队（主线程）：同模式未完成/未排队时合并丢弃，跨模式排队串行执行不互饿
    private func enqueue(mode: String, args: [String], completion: @escaping (DashData) -> Void) {
        guard !queuedModes.contains(mode) else {
            dashLog("fetch 合并丢弃 mode=" + mode)   // 该模式已有一次在途/排队，结果足够新
            return
        }
        queuedModes.insert(mode)
        fetchQueue.async { [self] in
            let data = runFetch(args: args)
            DispatchQueue.main.async { [self] in
                queuedModes.remove(mode)
                completion(data)
            }
        }
    }

    private func runFetch(args: [String]) -> DashData {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: venvPython)
        // 未设 AIUSD_PYTHON 时经 /usr/bin/env shim 走 PATH 里的 python3
        proc.arguments = usesEnvShim ? ["python3", fetchScript] + args
                                     : [fetchScript] + args
        // 禁止子进程写 .pyc：避免运行时在 bundle Resources/ 里生成 __pycache__
        var childEnv = ProcessInfo.processInfo.environment
        childEnv["PYTHONDONTWRITEBYTECODE"] = "1"
        // GUI 设置（设置窗口显式配置项）优先于继承的 shell 环境
        for (k, v) in SettingsStore.envOverrides() { childEnv[k] = v }
        proc.environment = childEnv

        let outPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = Pipe()   // 丢弃 stderr，不污染

        do {
            try proc.run()
        } catch {
            return .failure("spawn失败: \(error.localizedDescription)")
        }

        // 超时兜底：terminate 后 readDataToEndOfFile 会返回
        var timedOut = false
        let timeoutWork = DispatchWorkItem { [weak proc] in
            timedOut = true
            proc?.terminate()
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + fetchTimeout, execute: timeoutWork)
        defer { timeoutWork.cancel() }

        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()

        if timedOut {
            return .failure("采集超时(\(Int(fetchTimeout))s)")
        }
        // 脚本失败(exit 1)仍输出 ok:false JSON，优先解析它拿 error 字段
        if let parsed = parse(outData) {
            return parsed
        }
        return .failure("JSON解析失败, exit=\(proc.terminationStatus)")
    }

    private func parse(_ data: Data) -> DashData? {
        try? JSONDecoder().decode(DashData.self, from: data)
    }
}

// MARK: - 文件系统事件监控（6 个 agent 日志目录组，事件驱动刷新）

final class FSEventMonitor {
    /// 数据目录组 → 工具 key（事件路径以任一监听路径为前缀即归属该工具）
    private let groups: [(tool: String, paths: [String])] = [
        ("codex", ["~/.codex/sessions", "~/.codex/archived_sessions"]
            .map { ($0 as NSString).expandingTildeInPath }),
        ("claude", ["~/.claude/projects"].map { ($0 as NSString).expandingTildeInPath }),
        ("hermes", ["~/.hermes/logs"].map { ($0 as NSString).expandingTildeInPath }),
        ("trae", ["~/Library/Application Support/Trae CN/User/"
                  + "globalStorage/firerl.trae-use-token"]
            .map { ($0 as NSString).expandingTildeInPath }),
        ("zcode", ["~/.zcode/cli"]
            .map { ($0 as NSString).expandingTildeInPath }),
        ("workbuddy", ["~/.workbuddy/traces", "~/.workbuddy/projects"]
            .map { ($0 as NSString).expandingTildeInPath }),
    ]

    private var stream: FSEventStreamRef?
    private let queue = DispatchQueue(label: "fs.event.monitor", qos: .utility)
    private var pendingTools = Set<String>()          // 防抖窗口内累积的触发工具
    private var debounceWork: DispatchWorkItem?
    private var lastRefresh = Date.distantPast
    private let debounce: TimeInterval = 4            // 日志连续写入合并窗口
    private let minInterval: TimeInterval = 30        // 两次刷新最小间隔
    private var handler: ((Set<String>) -> Void)?

    func start(handler: @escaping (Set<String>) -> Void) {
        self.handler = handler
        let allPaths = groups.flatMap { $0.paths }
        dashLog("FSEvent start paths=\(allPaths)")
        var ctx = FSEventStreamContext(
            version: 0,
            info: Unmanaged.passUnretained(self).toOpaque(),
            retain: nil, release: nil, copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, _, rawPaths, _, _ in
            guard let info = info else { return }
            let monitor = Unmanaged<FSEventMonitor>.fromOpaque(info).takeUnretainedValue()
            // kFSEventStreamCreateFlagUseCFTypes 下 eventPaths 是 CFArrayRef（借用，不 release）
            let cf = Unmanaged<CFArray>.fromOpaque(rawPaths).takeUnretainedValue()
            monitor.handleEvents(paths: (cf as? [String]) ?? [])
        }
        stream = FSEventStreamCreate(
            kCFAllocatorDefault, callback, &ctx,
            allPaths as CFArray, FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
            1.0,   // latency：1s 聚合窗口（配合自研 debounce 二次合并）
            FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents
                                     | kFSEventStreamCreateFlagNoDefer
                                     | kFSEventStreamCreateFlagUseCFTypes))
        if let stream = stream {
            FSEventStreamSetDispatchQueue(stream, queue)
            FSEventStreamStart(stream)
            dashLog("FSEvent stream started")
        } else {
            dashLog("FSEvent stream CREATE FAILED")
        }
    }

    func stop() {
        if let stream = stream {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
            self.stream = nil
        }
        debounceWork?.cancel()
    }

    /// 事件回调（utility 队列）：映射到工具 → 累积进 pendingTools → 防抖调度
    private func handleEvents(paths: [String]) {
        var touched = Set<String>()
        for p in paths {
            for g in groups where g.paths.contains(where: { p.hasPrefix($0) }) {
                touched.insert(g.tool)
            }
        }
        dashLog("event paths=\(paths.count) touched=\(touched)")
        guard !touched.isEmpty else { return }
        pendingTools.formUnion(touched)
        schedule()
    }

    private func schedule() {
        let now = Date()
        let earliest = lastRefresh.addingTimeInterval(minInterval)
        // 距上次刷新不足 minInterval 时顺延，保证最小间隔
        let delay = max(debounce, earliest.timeIntervalSince(now))
        debounceWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            let tools = self.pendingTools
            self.pendingTools.removeAll()
            self.lastRefresh = Date()
            dashLog("TRIGGER tools=\(tools)")
            self.handler?(tools)
        }
        debounceWork = work
        queue.asyncAfter(deadline: .now() + delay, execute: work)
    }
}

// MARK: - 调试日志（/tmp/dash_dash.log，追加，用于诊断刷新链路）

func dashLog(_ msg: String) {
    let f = "/tmp/dash_dash.log"
    let line = "\(Date().formatted(date: .omitted, time: .standard)) \(msg)\n"
    if let h = FileHandle(forWritingAtPath: f) {
        h.seekToEndOfFile()
        h.write(line.data(using: .utf8)!)
        try? h.close()
    } else {
        try? line.write(toFile: f, atomically: true, encoding: .utf8)
    }
}

// MARK: - 颜色与格式化工具（keydash 定稿规则）

func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
    // srgbRed：与 keydash PIL 渲染的 RGB 直值精确一致（calibratedRed 会 gamma 偏移变亮）
    NSColor(srgbRed: r/255, green: g/255, blue: b/255, alpha: a)
}

/// ≥1000万统一显示为 0.xx亿；≥1万显示 xx.x万（防长数字溢出）
/// ⚠️ 过滤只用 ASCII 数字：Swift Character.isNumber 对 CJK 字符（如"万"）也返回 true，
/// 会把单位混进数字导致 Double 解析失败返回 0（2026-08-21 实测坑）
func fmtTokens(_ txt: String) -> String {
    let cleaned = txt.filter { ($0.isASCII && $0.isNumber) || $0 == "." }
    guard let val = Double(cleaned) else { return txt }
    var value = val
    if txt.contains("亿") { value = val * 1e8 }
    else if txt.contains("万") { value = val * 1e4 }
    if value >= 1e7 { return String(format: "%.2f亿", value / 1e8) }
    if value >= 1e4 { return String(format: "%.1f万", value / 1e4) }
    return "\(Int(value))"
}

/// 解析 "2622.3万"/"0.41亿" → 亿单位的 Double（同 ASCII 过滤坑）
func parseYi(_ txt: String) -> Double {
    let cleaned = txt.filter { ($0.isASCII && $0.isNumber) || $0 == "." }
    guard let val = Double(cleaned) else { return 0 }
    if txt.contains("亿") { return val }
    if txt.contains("万") { return val / 1e4 }
    return val / 1e8
}

/// 合计数字分级变色：<1亿绿 / ≥1亿蓝 / ≥3亿金 / ≥5亿橙 / ≥8亿红 / ≥10亿深红 / ≥12亿紫 / ≥15亿勃艮第红 / ≥18亿熔岩霓虹红 / ≥21亿霓虹品红
func totalColor(_ yi: Double) -> NSColor {
    if yi >= 21 { return rgb(255, 20, 147) }
    if yi >= 18 { return rgb(255, 45, 85) }
    if yi >= 15 { return rgb(190, 20, 60) }
    if yi >= 12 { return rgb(200, 130, 255) }
    if yi >= 10 { return rgb(255, 70, 70) }
    if yi >= 8  { return rgb(255, 100, 90) }
    if yi >= 5  { return rgb(255, 155, 70) }
    if yi >= 3  { return rgb(255, 209, 102) }
    if yi >= 1  { return rgb(110, 180, 255) }
    return rgb(124, 232, 181)
}

/// 统一文本绘制入口：返回文本尺寸（供布局计算）
@discardableResult
func drawText(_ text: String, at p: NSPoint, font: NSFont, color: NSColor) -> NSSize {
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
    (text as NSString).draw(at: p, withAttributes: attrs)
    return (text as NSString).size(withAttributes: attrs)
}

// MARK: - 布局常量（pt）—— macOS 桌面竖版，四张正方形卡片竖排 + 底部合计条

enum Layout {
    static let side: CGFloat = 140      // 正方形卡片边长（宽 = 高）
    static let pad: CGFloat = 10
    static let gap: CGFloat = 8         // 卡间与卡→合计条间距一致
    static let totalH: CGFloat = 60     // 合计条三行（keydash 式：标签/数字/时间）
    static let radius: CGFloat = 20     // 经典模式圆角（2026-08-30 与玻璃模式 glassRadius 统一）
    static let glassRadius: CGFloat = 20  // 玻璃模式圆角（官方小组件风格，更圆润）
    static var w: CGFloat { side + 2 * pad }    // = 160

    /// 窗口高度随可见卡片数自适应：pad + N 卡(含卡→卡及卡→合计条 gap) + 合计条 + pad
    static func windowH(cards: Int) -> CGFloat {
        pad + CGFloat(cards) * (side + gap) + totalH + pad
    }
}

// MARK: - Logo 资源（经典/玻璃两种模式共用）

enum LogoStore {
    static let shared: [String: NSImage] = {
        var m: [String: NSImage] = [:]
        // 图标目录：仓库/bundle 双布局定位（见 Layout），可用环境变量覆盖
        let dir = BundleLayout.assetsIconsDir
        let files = ["codex": "codex-mark-hq.png",
                     "claude": "claude-512.png",
                     "hermes": "hermes-1024.png",
                     "trae": "trae-256.png",
                     "zcode": "zcode-256.png",
                     "workbuddy": "workbuddy-256.png"]
        for (k, f) in files {
            if let img = NSImage(contentsOfFile: dir + "/" + f) {
                img.size = NSSize(width: 20, height: 20)
                m[k] = img
            }
        }
        return m
    }()
}

// MARK: - 工具卡内容绘制（透明背景；经典模式画在渐变底上，玻璃模式画在玻璃上）

final class ToolCardContentView: NSView {
    var info: ToolUsage? { didSet { needsDisplay = true } }
    /// 玻璃增亮模式：玻璃背景较亮，次级文字需提高 alpha 保证可读（经典渐变模式保持原值）
    var bright = false
    private let key: String
    private var logo: NSImage? { LogoStore.shared[key] }

    init(key: String) {
        self.key = key
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override var isFlipped: Bool { true }   // 左上原点

    override func draw(_ dirtyRect: NSRect) {
        guard let info = info else { return }
        let px: CGFloat = 10        // 左内边距 10pt（与合计条对齐，贴近键盘屏 9pt）
        let rightX = bounds.width - 10

        // 行1：logo + 名称（左对齐，垂直居中于 logo 行）
        if let logo = logo {
            logo.draw(in: NSRect(x: px, y: 15, width: 20, height: 20))
        }
        let nameText = info.name ?? "--"
        let nameAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 12, weight: .semibold),
            .foregroundColor: NSColor.white]
        let nameSize = (nameText as NSString).size(withAttributes: nameAttrs)
        (nameText as NSString).draw(at: NSPoint(x: px + 24, y: 15 + (20 - nameSize.height) / 2),
                                    withAttributes: nameAttrs)

        // 行2：token 大数字（左对齐）
        let numTxt = fmtTokens(info.tokens ?? "--")
        let numAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 22, weight: .bold),
            .foregroundColor: NSColor.white]
        (numTxt as NSString).draw(at: NSPoint(x: px, y: 46), withAttributes: numAttrs)

        // 行3：轮次 · 模型保持单行；模型按剩余宽度截断，额度区位置保持不变
        // 仅显示层去掉轮次千分位逗号，为完整模型名腾出空间。
        let turnsTxt = (info.turns ?? "--").replacingOccurrences(of: ",", with: "")
        let modelTxt = info.model ?? "--"
        let infoFont = NSFont.systemFont(ofSize: 9)
        // 玻璃模式白字全不透明（用户要求），经典模式保持原 62% 层级
        let infoColor = NSColor.white.withAlphaComponent(bright ? 1.0 : 0.62)
        let availW = rightX - px
        let singleTxt = "\(turnsTxt) · \(modelTxt)"
        let singleW = (singleTxt as NSString).size(withAttributes: [.font: infoFont]).width
        let infoY: CGFloat = 76
        var displayTxt = singleTxt
        if singleW > availW {
            // 从右端裁剪：优先保留轮次，只有实际截断时才显示省略号。
            while !displayTxt.isEmpty,
                  (displayTxt + "…" as NSString).size(withAttributes: [.font: infoFont]).width > availW {
                displayTxt.removeLast()
            }
            displayTxt += "…"
        }
        drawText(displayTxt, at: NSPoint(x: px, y: infoY), font: infoFont, color: infoColor)

        // 行4-5：额度区（Codex：单行周额度；ZCode：双行 5h 窗 + 周窗；
        // Claude/Hermes/Trae/WorkBuddy 无额度显示——WorkBuddy 定稿只显 token 消耗）
        if let q = info.quota_left, key != "workbuddy" {
            if info.quota_5h_left != nil {
                drawDualQuotaRows(info, atX: px, y: 90, rightX: rightX)
            } else {
                drawQuotaRow(info, q: q, atX: px, y: 106, rightX: rightX)
            }
        }
    }

    /// ZCode 双块额度：布局复刻 Codex 卡 drawQuotaRow（行1 文字+进度条、行2 重置时间
    /// 在 +16pt 处——与 Codex 完全同行距）。5h 块无行2、周块在上移后的 y=92 起，
    /// 两块总高到底部 ≈137 < 卡高 140。
    private func drawDualQuotaRows(_ info: ToolUsage, atX px: CGFloat, y: CGFloat, rightX: CGFloat) {
        let rows: [(String, Int, String?)] = [
            ("5h", info.quota_5h_left ?? 0, nil as String?),   // 用户要求：只显示周重置时间
            ("周", info.quota_left ?? 0, info.reset.map { "重置 \($0)" }),
        ]
        let textAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 9),
            .foregroundColor: NSColor.white.withAlphaComponent(bright ? 1.0 : 0.85)]
        // 两行进度条左端对齐：barX 取两行标签宽的较大值（"周剩余"比"5h剩余"略宽）
        var labelColW: CGFloat = 0
        for (label, left, _) in rows {
            let w = ("\(label)剩余 \(left)%" as NSString)
                .size(withAttributes: textAttrs).width
            labelColW = max(labelColW, w)
        }
        let barX = px + labelColW + 6
        let bw = max(rightX - barX, 24)
        var rowY = y
        for (label, left, reset) in rows {
            // 5h 行（无重置时间）整体下移 2pt，与周块视觉对齐
            let yOffset: CGFloat = reset == nil ? 4 : 0   // 5h 行下移 4pt，视觉与周块平衡
            let leftTxt = "\(label)剩余 \(left)%"
            (leftTxt as NSString).draw(at: NSPoint(x: px, y: rowY - 1 + yOffset),
                                       withAttributes: textAttrs)

            let bh: CGFloat = 5
            let barY = rowY + 1 + yOffset
            rgb(235, 68, 58).setFill()
            NSBezierPath(roundedRect: NSRect(x: barX, y: barY, width: bw, height: bh),
                         xRadius: bh / 2, yRadius: bh / 2).fill()
            if left > 0 {
                let filled = min(max(CGFloat(left) / 100 * bw, 3), bw)
                rgb(94, 234, 148).setFill()
                NSBezierPath(roundedRect: NSRect(x: barX, y: barY, width: filled, height: bh),
                             xRadius: bh / 2, yRadius: bh / 2).fill()
            }
            // 行2：完整重置时间（+16pt，与 Codex 卡 drawQuotaRow 同行距）。
            // reset 传入时已带「重置」前缀；快照数据（Claude）按 Codex 惯例
            // 显示采样时间替代，避免误当实时值
            if let reset = reset {
                let line2 = (info.quota_snapshot_ts != nil)
                    ? "快照 \(info.quota_snapshot_ts ?? "--")" : reset
                _ = drawText(line2, at: NSPoint(x: px, y: rowY + 16),
                             font: .systemFont(ofSize: 8.5),
                             color: NSColor.white.withAlphaComponent(bright ? 1.0 : 0.55))
            }
            rowY += (reset == nil ? 18 : 30)   // 5h 行无行2 → 紧凑；周块 = 行距16 + 块间14
        }
    }

    /// 周剩余文字 + 双色进度条（keydash 同款：文字与条同行，重置/快照在下一行）
    /// 行1 顶 = y-1，行2 顶 = y+16（行距 6pt，配额区垂直居中）
    private func drawQuotaRow(_ info: ToolUsage, q: Int, atX px: CGFloat, y: CGFloat, rightX: CGFloat) {
        let isSnapshot = info.quota_snapshot_ts != nil
        // WorkBuddy 额度=资源包 credit（非周窗口径），标签用「额度剩余」
        let label = key == "workbuddy" ? "额度剩余" : "周剩余"
        let leftTxt = isSnapshot ? "\(label) ~\(q)%" : "\(label) \(q)%"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 9),
            .foregroundColor: NSColor.white.withAlphaComponent(bright ? 1.0 : 0.85)]
        let ts = (leftTxt as NSString).size(withAttributes: attrs)
        (leftTxt as NSString).draw(at: NSPoint(x: px, y: y - 1), withAttributes: attrs)

        let bx = px + ts.width + 6
        let bw = rightX - bx
        let bh: CGFloat = 5
        let barY = y + 1
        rgb(235, 68, 58).setFill()
        NSBezierPath(roundedRect: NSRect(x: bx, y: barY, width: bw, height: bh),
                     xRadius: bh / 2, yRadius: bh / 2).fill()
        if q > 0 {
            let filled = min(max(CGFloat(q) / 100 * bw, 3), bw)
            rgb(94, 234, 148).setFill()
            NSBezierPath(roundedRect: NSRect(x: bx, y: barY, width: filled, height: bh),
                         xRadius: bh / 2, yRadius: bh / 2).fill()
        }

        // 重置/快照时间（下一行，keydash 同款；快照时内容即标识）
        let rightTxt = isSnapshot ? "快照 \(info.quota_snapshot_ts ?? "--")" : "重置 \(info.reset ?? "--")"
        _ = drawText(rightTxt, at: NSPoint(x: px, y: y + 16),
                     font: .systemFont(ofSize: 8.5),
                     color: NSColor.white.withAlphaComponent(bright ? 1.0 : 0.55))
    }
}

// MARK: - 合计条内容绘制（三行居中：标签 / 大数字+单位 / 更新时间）

final class TotalBarContentView: NSView {
    var data: DashData? { didSet { needsDisplay = true } }
    /// 玻璃增亮模式（同 ToolCardContentView.bright）
    var bright = false

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        guard let data = data else {
            let t = "AI 用量加载中…"
            let attrs: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 9),
                .foregroundColor: NSColor.white.withAlphaComponent(bright ? 1.0 : 0.6)]
            let s = (t as NSString).size(withAttributes: attrs)
            (t as NSString).draw(at: NSPoint(x: bounds.midX - s.width / 2,
                                             y: bounds.midY - s.height / 2),
                                 withAttributes: attrs)
            return
        }
        let midX = bounds.midX

        // 行1：标签（keydash 同款文案，9pt 灰，居中）
        let label = "今日消耗token合计"
        let labelAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 9),
            .foregroundColor: bright ? rgb(205, 214, 232) : rgb(160, 172, 196)]
        let ls = (label as NSString).size(withAttributes: labelAttrs)
        (label as NSString).draw(at: NSPoint(x: midX - ls.width / 2, y: 6),
                                 withAttributes: labelAttrs)

        // 行2：大数字（分级变色）+ " tokens"（整体居中，行内垂直居中）
        let row2Y: CGFloat = 22
        let row2H: CGFloat = 19          // 16pt 数字行高基准

        let numTxt = fmtTokens(data.total ?? "--")
        let numAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 16, weight: .bold),
            .foregroundColor: totalColor(parseYi(numTxt))]
        let ns = (numTxt as NSString).size(withAttributes: numAttrs)

        let unitTxt = "tokens"
        let unitAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 9), .foregroundColor: NSColor.white]
        let us = (unitTxt as NSString).size(withAttributes: unitAttrs)

        let rowW = ns.width + 5 + us.width
        let startX = midX - rowW / 2
        (numTxt as NSString).draw(at: NSPoint(x: startX, y: row2Y), withAttributes: numAttrs)
        (unitTxt as NSString).draw(at: NSPoint(x: startX + ns.width + 5,
                                               y: row2Y + (row2H - us.height) / 2),
                                   withAttributes: unitAttrs)

        // 行3：更新时间（居中）
        let timeTxt = data.ok ? "更新时间 \(data.time ?? "--")" : "采集失败 · 显示上次数据"
        let timeAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 8.5),
            .foregroundColor: bright ? rgb(178, 186, 204) : rgb(130, 138, 158)]
        let ts = (timeTxt as NSString).size(withAttributes: timeAttrs)
        (timeTxt as NSString).draw(at: NSPoint(x: midX - ts.width / 2, y: 45),
                                   withAttributes: timeAttrs)
    }
}

// MARK: - 经典模式主视图（三张渐变卡片竖排 + 底部合计条；液态玻璃前版本）

final class DashView: NSView {
    var data: DashData? { didSet { needsDisplay = true } }

    override var isFlipped: Bool { true }   // 左上原点，与 PIL 渲染一致的直觉

    override func draw(_ dirtyRect: NSRect) {
        guard let data = data else {
            drawPlaceholder()
            return
        }
        // 六张正方形卡片竖排（按可见性过滤，顺序固定）
        let allCards: [(ToolUsage?, String, (NSColor, NSColor))] = [
            (data.codex, "codex", (rgb(150, 100, 250), rgb(100, 50, 215))),
            (data.claude, "claude", (rgb(255, 140, 50), rgb(205, 70, 15))),
            (data.hermes, "hermes", (rgb(52, 63, 84), rgb(22, 30, 46))),
            (data.trae, "trae", (rgb(94, 234, 148), rgb(14, 110, 62))),
            (data.zcode, "zcode", (rgb(70, 160, 235), rgb(15, 80, 150))),
            (data.workbuddy, "workbuddy", (rgb(14, 200, 169), rgb(1, 163, 118))),
        ]
        let rows = allCards.filter { AiUsageDashApp.visibleTools.contains($0.1) }
        var y = Layout.pad
        for (info, key, colors) in rows {
            drawToolCard(info, key: key, colors: colors, atY: y)
            y += Layout.side + Layout.gap
        }
        // 底部合计条（与最后一张卡保持与卡间相同的 gap）
        drawTotalBar(data, atY: y)
    }

    // MARK: 正方形工具卡（keydash 左对齐排版 + 高光/描边精致化）

    private func drawToolCard(_ info: ToolUsage?, key: String,
                              colors: (NSColor, NSColor), atY y: CGFloat) {
        let rect = NSRect(x: Layout.pad, y: y, width: Layout.side, height: Layout.side)
        let path = NSBezierPath(roundedRect: rect, xRadius: Layout.radius, yRadius: Layout.radius)
        NSGraphicsContext.saveGraphicsState()
        path.addClip()
        NSGradient(starting: colors.0, ending: colors.1)!.draw(in: rect, angle: 90)  // 实测：90 = c1 顶部 → c2 底部
        // 顶部高光：白色 16% → 透明，增强立体感（keydash 无，桌面版精致化）
        NSGradient(starting: NSColor.white.withAlphaComponent(0.16),
                   ending: NSColor.white.withAlphaComponent(0))!
            .draw(in: NSRect(x: rect.minX, y: rect.minY, width: rect.width, height: 28), angle: 90)
        NSGraphicsContext.restoreGraphicsState()

        // 细描边（白色 10%），圆角跟随，提升边缘精致度
        NSColor.white.withAlphaComponent(0.10).setStroke()
        path.lineWidth = 1
        path.stroke()

        // 内容（复用 ToolCardContentView 同款排版：临时视图手动绘制到当前上下文）
        // ⚠️ 父视图 draw() 上下文已是 flipped（顶左原点，y 向下），手动调 draw() 时
        // isFlipped 不再次生效——只需平移到卡片左上角，坐标语义即与内容视图一致；
        // 额外 Y 翻转会把文字画成上下镜像（2026-08-26 修正）
        guard info != nil else { return }
        let content = ToolCardContentView(key: key)
        content.info = info
        content.frame = NSRect(x: rect.minX, y: rect.minY, width: rect.width, height: rect.height)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current?.cgContext.translateBy(x: rect.minX, y: rect.minY)
        content.draw(content.bounds)
        NSGraphicsContext.restoreGraphicsState()
    }

    // MARK: 合计条（经典模式：深灰实底）

    private func drawTotalBar(_ data: DashData, atY y: CGFloat) {
        let rect = NSRect(x: Layout.pad, y: y, width: Layout.w - 2 * Layout.pad, height: Layout.totalH)
        let path = NSBezierPath(roundedRect: rect, xRadius: Layout.radius, yRadius: Layout.radius)
        rgb(30, 40, 62).setFill()
        path.fill()
        // 无描边（与卡片视觉一致；实测 withAlphaComponent 描边在此渲染路径下
        // alpha 丢失会画成 1px 纯白线，深色底上显眼，故直接不画）

        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current?.cgContext.translateBy(x: rect.minX, y: rect.minY)
        let content = TotalBarContentView()
        content.data = data
        content.frame = rect
        content.draw(content.bounds)
        NSGraphicsContext.restoreGraphicsState()
    }

    private func drawPlaceholder() {
        _ = drawText("AI 用量加载中…", at: NSPoint(x: 20, y: 16),
                     font: .systemFont(ofSize: 13), color: NSColor.white.withAlphaComponent(0.6))
    }
}

// MARK: - 玻璃模式内容（每卡一个 NSGlassEffectView，macOS 26 原生液态玻璃）

enum GlassTransparency: String, CaseIterable {
    case clear, standard, rich

    var title: String {
        switch self {
        case .clear: return "清透"
        case .standard: return "标准"
        case .rich: return "浓郁"
        }
    }

    /// tint 全档恒定：其冷偏置（被材质洗向灰蓝）是加性常量，档间比较时抵消。
    /// ⚠️ 若随档位缩放 tint，清透档冷偏置相对占优 → 切档整卡偏冷（2026-10-30 夜实证）
    var tintAlpha: CGFloat { 0.12 }

    /// 档位只缩放叠层（品牌色，线性层）：清透档 = 标准档的去饱和版，色相方向不漂移。
    /// ⚠️ 叠层绝对量必须随档位变化而非配比变化——材质合成非线性，配比恒定仍会色温漂移
    var overlayAlpha: (top: CGFloat, bottom: CGFloat) {
        switch self {
        case .clear: return (0.065, 0.11)
        case .standard: return (0.10, 0.17)
        case .rich: return (0.16, 0.27)
        }
    }
}

final class BrandTintOverlay: NSView {
    var top: NSColor = .clear
    var bottom: NSColor = .clear
    var corner: CGFloat = 20
    override var isFlipped: Bool { true }
    /// 关闭图层后备渲染：实测 wantsLayer 路径下半透明渐变 alpha 会丢失，
    /// 把品牌渐变画成实色（盖死玻璃质感）；纯 draw() 路径 alpha 正常合成
    override var wantsLayer: Bool {
        get { false } set { }
    }
    override func draw(_ dirtyRect: NSRect) {
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(roundedRect: bounds, xRadius: corner, yRadius: corner).addClip()
        NSGradient(starting: top, ending: bottom)!.draw(in: bounds, angle: 90)
        NSGraphicsContext.restoreGraphicsState()
    }
}

final class GlassDashContent: NSView {
    private(set) var codexCard = ToolCardContentView(key: "codex")
    private(set) var claudeCard = ToolCardContentView(key: "claude")
    private(set) var hermesCard = ToolCardContentView(key: "hermes")
    private(set) var traeCard = ToolCardContentView(key: "trae")
    private(set) var zcodeCard = ToolCardContentView(key: "zcode")
    private(set) var workbuddyCard = ToolCardContentView(key: "workbuddy")
    private(set) var totalBar = TotalBarContentView()
    /// key → 玻璃容器视图（init 时全部创建，可见性切换由 relayout 隐藏/显示）
    private var cardViews: [String: NSView] = [:]
    private var totalView: NSView?

    override var isFlipped: Bool { true }

    init() {
        super.init(frame: NSRect(x: 0, y: 0, width: Layout.w, height: Layout.windowH(cards: AiUsageDashApp.toolOrder.count)))
        // 玻璃增亮：次级文字提高可读性（经典模式保持原 alpha）
        codexCard.bright = true
        claudeCard.bright = true
        hermesCard.bright = true
        traeCard.bright = true
        zcodeCard.bright = true
        workbuddyCard.bright = true
        totalBar.bright = true
        let cardDefs: [(String, ToolCardContentView, NSColor, (NSColor, NSColor))] = [
            ("codex", codexCard, rgb(130, 80, 248),
             (rgb(150, 100, 250), rgb(100, 50, 215))),
            ("claude", claudeCard, rgb(242, 105, 25),
             (rgb(255, 140, 50), rgb(205, 70, 15))),
            ("hermes", hermesCard, rgb(40, 50, 70),
             (rgb(52, 63, 84), rgb(22, 30, 46))),
            ("trae", traeCard, rgb(20, 172, 100),
             (rgb(60, 200, 120), rgb(15, 115, 65))),
            ("zcode", zcodeCard, rgb(25, 125, 215),
             (rgb(70, 160, 235), rgb(15, 80, 150))),
            ("workbuddy", workbuddyCard, rgb(0, 205, 152),
             (rgb(25, 195, 160), rgb(5, 140, 100))),
        ]
        for (key, content, tint, grad) in cardDefs {
            let g = makeGlass(content: content, tint: tint, grad: grad,
                              rect: NSRect(x: Layout.pad, y: 0, width: Layout.side,
                                           height: Layout.side))
            cardViews[key] = g
            addSubview(g)
        }
        // 合计条玻璃（深灰品牌渐变）
        let t = makeGlass(content: totalBar, tint: rgb(30, 40, 62),
                          grad: (rgb(42, 54, 80), rgb(24, 32, 50)),
                          rect: NSRect(x: Layout.pad, y: 0, width: Layout.side,
                                       height: Layout.totalH))
        totalView = t
        addSubview(t)
        relayout()   // 按当前可见性统一定位
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// 原生小组件同款材质:私有 variant=4(Widgets),社区逆向成果(electron-liquid-glass 系)
    /// 有 responds 守卫,未来 macOS 移除该 setter 时静默回退公开 .regular
    private func applyGlassProbeHooks(_ v: NSGlassEffectView, transparency: GlassTransparency) {
        setPrivateInt(v, "variant", 4)   // Widgets 材质
        setPrivateInt(v, "scrimState", 1) // 固定压暗层：削弱壁纸色温透入权重（2026-10-30 实测均匀 -3~5）
        let env = ProcessInfo.processInfo.environment
        if let s = env["GLASS_SCRIM"], let n = Int(s) { setPrivateInt(v, "scrimState", n) }
        if let s = env["GLASS_SUBDUED"], let n = Int(s) { setPrivateInt(v, "subduedState", n) }
    }

    private func setPrivateInt(_ obj: NSObject, _ key: String, _ value: Int) {
        let sel = NSSelectorFromString("set_\(key):")
        guard obj.responds(to: sel) else { dashLog("private setter missing: \(key)"); return }
        let imp = obj.method(for: sel)
        typealias Fn = @convention(c) (NSObject, Selector, Int) -> Void
        (unsafeBitCast(imp, to: Fn.self))(obj, sel, value)
    }

    private func makeGlass(content: NSView, tint: NSColor,
                           grad: (NSColor, NSColor), rect: NSRect) -> NSView {
        let transparency = AiUsageDashApp.glassTransparency
        let g = NSGlassEffectView()
        g.cornerRadius = Layout.glassRadius
        g.style = .regular
        g.tintColor = tint.withAlphaComponent(transparency.tintAlpha)
        let inner = NSView(frame: NSRect(x: 0, y: 0, width: rect.width, height: rect.height))
        let overlay = BrandTintOverlay()
        overlay.top = grad.0.withAlphaComponent(transparency.overlayAlpha.top)
        overlay.bottom = grad.1.withAlphaComponent(transparency.overlayAlpha.bottom)
        overlay.corner = Layout.glassRadius
        overlay.frame = inner.bounds
        overlay.autoresizingMask = [.width, .height]
        inner.addSubview(overlay)
        content.frame = inner.bounds
        content.autoresizingMask = [.width, .height]
        inner.addSubview(content)
        g.contentView = inner
        applyGlassProbeHooks(g, transparency: transparency)
        g.frame = rect
        return g
    }

    func update(_ data: DashData?) {
        codexCard.info = data?.codex
        claudeCard.info = data?.claude
        hermesCard.info = data?.hermes
        traeCard.info = data?.trae
        zcodeCard.info = data?.zcode
        workbuddyCard.info = data?.workbuddy
        totalBar.data = data
    }

    /// 按当前可见性隐藏/显示卡片并重新排布（勾选切换时调用；不重建窗口内容）
    func relayout() {
        var y = Layout.pad
        for key in AiUsageDashApp.toolOrder {
            guard let g = cardViews[key] else { continue }
            let visible = AiUsageDashApp.visibleTools.contains(key)
            g.isHidden = !visible
            if visible {
                g.frame = NSRect(x: Layout.pad, y: y, width: Layout.side, height: Layout.side)
                y += Layout.side + Layout.gap
            }
        }
        totalView?.isHidden = false
        totalView?.frame = NSRect(x: Layout.pad, y: y, width: Layout.side, height: Layout.totalH)
    }
}

// MARK: - 悬浮面板

final class DashPanel: NSPanel {
    init() {
        let rect = NSRect(x: 0, y: 0, width: Layout.w,
                          height: Layout.windowH(cards: AiUsageDashApp.visibleTools.count))
        super.init(contentRect: rect,
                   styleMask: [.borderless, .nonactivatingPanel],  // 不抢焦点
                   backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        // 钉死浅色外观：玻璃材质不再随系统昼夜自动翻转（消除卡片色温漂移的主因）
        // ⚠️ 选 aqua 而非 darkAqua：darkAqua 材质实测整列压暗 ~30 色阶，偏离用户批准的亮度基准
        appearance = NSAppearance(named: .aqua)
        level = .floating                       // main 里按模式切换（默认贴桌面）
        // 仅位于桌面：不 join 全屏 space（去 fullScreenAuxiliary），全屏 App 时窗口不显示；
        // canJoinAllSpaces 保证普通多桌面都可见；
        // stationary：不参与 Mission Control 窗口整理/动画（缓解切换桌面时窗口闪烁重排）
        collectionBehavior = [.canJoinAllSpaces, .stationary]
        hidesOnDeactivate = false
        animationBehavior = .none        // 关闭窗口显示/隐藏动画：Space/全屏切换重挂载时更干脆，减少闪烁感
        // 锁定：禁止拖动（isMovable 整体禁拖；isMovableByWindowBackground 防背景拖）
        isMovable = !AiUsageDashApp.locked
        isMovableByWindowBackground = !AiUsageDashApp.locked
        isReleasedWhenClosed = false
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}

// MARK: - 右键菜单

extension NSWindow.Level {
    /// 桌面便签层：桌面图标之上、普通窗口之下。
    /// 注意不能用 .desktopWindowLevel（在 Finder 桌面之下，点击全被 Finder 桌面拦截，
    /// 窗口永远无法交互/拖动）。desktopIconWindow + 1 即可交互，又被普通窗口盖住。
    static var desktopPinnedLevel: NSWindow.Level {
        NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
    }
}

// MARK: - 设置存储与 env 桥（GUI 配置 → 子进程环境变量）
//
// 语义：GUI 未配置（空）= 不注入 = Python 层默认行为，与 CLI/launchd 用户完全
// 一致；显式配置才覆盖（含覆盖继承的 shell 环境）。Python 采集层零改动。
enum SettingsStore {
    struct Field {
        let key: String          // UserDefaults 键
        let env: String          // 注入的子进程环境变量
        let label: String
        let placeholder: String
    }
    /// 核心项（推送与归档）+ 高级项；顺序即设置窗口展示顺序
    static let pushFields: [Field] = [
        Field(key: "settings.deviceHost",  env: "AIUSD_DEVICE_HOST", label: "小屏设备 IP",
              placeholder: "如 192.168.1.50，留空不推送"),
        Field(key: "settings.nasVolume",   env: "AIUSD_NAS_VOLUME",  label: "NAS 挂载点",
              placeholder: "如 /Volumes/nas，留空跳过月归档"),
        Field(key: "settings.nasMountUrl", env: "AIUSD_NAS_MOUNT_URL", label: "NAS WebDAV URL",
              placeholder: "卷掉线时自动重挂，可留空"),
    ]
    static let advancedFields: [Field] = [
        Field(key: "settings.codexBin",        env: "AIUSD_CODEX_BIN",      label: "Codex CLI 路径",
              placeholder: "默认 PATH 上的 codex"),
        Field(key: "settings.codexAuditRoots", env: "AIUSD_CODEX_AUDIT_ROOTS", label: "audit 扫描根",
              placeholder: "冒号分隔 glob，默认不扫"),
        Field(key: "settings.hermesHome",      env: "HERMES_HOME",          label: "Hermes 数据目录",
              placeholder: "默认 ~/.hermes"),
        Field(key: "settings.stateDir",        env: "AIUSD_STATE_DIR",      label: "状态目录",
              placeholder: "默认 ~/.cache/ai_usage_dash"),
        Field(key: "settings.dataDir",         env: "AIUSD_DATA_DIR",       label: "统计 store 目录",
              placeholder: "默认 ~/Library/Application Support/AiUsageDash"),
    ]
    static func value(for key: String) -> String {
        UserDefaults.standard.string(forKey: key) ?? ""
    }
    static func envOverrides() -> [String: String] {
        var out: [String: String] = [:]
        for f in pushFields + advancedFields {
            let v = value(for: f.key).trimmingCharacters(in: .whitespaces)
            if !v.isEmpty { out[f.env] = v }
        }
        return out
    }
}

// MARK: - 依赖探测（设置窗口「依赖状态」区：pycryptodome / zstd）
enum DependencyProbe {
    /// 当前解释器（env AIUSD_PYTHON → 内嵌 runtime → PATH python3）能否 import Crypto
    static func probeCrypto(completion: @escaping (Bool) -> Void) {
        let f = DataFetcher.shared
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: f.venvPython)
        proc.arguments = f.usesEnvShim ? ["python3", "-c", "import Crypto"] : ["-c", "import Crypto"]
        proc.standardOutput = Pipe()   // 静默
        proc.standardError = Pipe()
        DispatchQueue.global(qos: .utility).async {
            let ok: Bool
            if (try? proc.run()) != nil {
                let timer = DispatchWorkItem { if proc.isRunning { proc.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + 8, execute: timer)
                proc.waitUntilExit()
                timer.cancel()
                ok = proc.terminationStatus == 0
            } else {
                ok = false
            }
            DispatchQueue.main.async { completion(ok) }
        }
    }
    /// zstd CLI（codex_usage.py 同款探测：which → Homebrew 兜底）
    static func zstdPath() -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        proc.arguments = ["which", "zstd"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        guard (try? proc.run()) != nil else { return nil }
        proc.waitUntilExit()
        if proc.terminationStatus == 0,
           let s = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
               .trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty {
            return s
        }
        for c in ["/opt/homebrew/bin/zstd", "/usr/local/bin/zstd"]
        where FileManager.default.fileExists(atPath: c) { return c }
        return nil
    }
    /// TCP 预检（与 keydash 推送前预检同语义：目标 80 端口毫秒级可达性）
    static func tcpProbe(host: String, timeout: TimeInterval = 3,
                         completion: @escaping (Bool) -> Void) {
        guard let port = NWEndpoint.Port(rawValue: 80) else { completion(false); return }
        let conn = NWConnection(host: NWEndpoint.Host(host), port: port, using: .tcp)
        var done = false
        let finish: (Bool) -> Void = { ok in
            guard !done else { return }
            done = true
            conn.cancel()
            DispatchQueue.main.async { completion(ok) }
        }
        conn.stateUpdateHandler = { st in
            switch st {
            case .ready: finish(true)
            case .failed, .cancelled: finish(false)
            default: break
            }
        }
        conn.start(queue: .global(qos: .userInitiated))
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { finish(false) }
    }
}

// MARK: - 设置窗口（通用 / 推送与归档 / 高级）
final class SettingsWindowController: NSObject, NSTextFieldDelegate {
    static let shared = SettingsWindowController()
    private var window: NSWindow?
    private var loginCheck: NSButton!
    private var loginStatus: NSTextField!
    private var freqPopup: NSPopUpButton!
    private var depLabel: NSTextField!
    private var probeResult: NSTextField!
    private var fields: [String: NSTextField] = [:]   // settings key → 输入框

    func show() {
        if window == nil { buildWindow() }
        syncStates()
        NSApp.activate(ignoringOtherApps: true)   // accessory app：显式抢一次焦点
        window?.makeKeyAndOrderFront(nil)
    }

    // MARK: 布局

    private func buildWindow() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 470),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "设置"
        w.isReleasedWhenClosed = false
        w.center()
        let tabs = NSTabView(frame: NSRect(x: 12, y: 46, width: 516, height: 410))
        tabs.addTabViewItem(makeGeneralTab(size: tabs.frame.size))
        tabs.addTabViewItem(makePushTab(size: tabs.frame.size))
        tabs.addTabViewItem(makeAdvancedTab(size: tabs.frame.size))
        w.contentView?.addSubview(tabs)
        window = w
    }

    private func makeTabItem(_ label: String, size: NSSize) -> (NSTabViewItem, NSView) {
        let item = NSTabViewItem(identifier: nil)
        item.label = label
        let view = NSView(frame: NSRect(x: 0, y: 0, width: size.width, height: size.height - 28))
        item.view = view
        return (item, view)
    }

    /// 一行「标签 + 控件」，y 自上而下递减；返回下一个 y
    @discardableResult
    private func addRow(_ y: CGFloat, label: String, control: NSControl?,
                        view: NSView, width: CGFloat = 320) -> CGFloat {
        let l = NSTextField(labelWithString: label)
        l.frame = NSRect(x: 14, y: y - 3, width: 130, height: 20)
        l.alignment = .right
        view.addSubview(l)
        if let c = control {
            c.frame = NSRect(x: 152, y: y, width: width, height: 22)
            view.addSubview(c)
        }
        return y - 34
    }

    private func textField(key: String, placeholder: String) -> NSTextField {
        let f = NSTextField()
        f.identifier = NSUserInterfaceItemIdentifier(key)
        f.placeholderString = placeholder
        f.stringValue = SettingsStore.value(for: key)
        f.delegate = self
        fields[key] = f
        return f
    }

    private func makeGeneralTab(size: NSSize) -> NSTabViewItem {
        let (item, view) = makeTabItem("通用", size: size)
        var y: CGFloat = view.frame.height - 40
        // 开机自启（SMAppService 登录项；app 移动位置后旧注册失效，重新勾选即可）
        let check = NSButton(checkboxWithTitle: "登录时自动启动", target: self,
                             action: #selector(toggleLoginItem))
        y = addRow(y, label: "常驻", control: check, view: view, width: 200)
        loginCheck = check
        loginStatus = NSTextField(labelWithString: "")
        loginStatus.font = NSFont.systemFont(ofSize: 11)
        loginStatus.textColor = .secondaryLabelColor
        loginStatus.frame = NSRect(x: 158, y: y + 12, width: 340, height: 16)
        view.addSubview(loginStatus)
        y -= 14
        // 刷新频率（与菜单共用 refreshIntervalMin 键，双向一致）
        let popup = NSPopUpButton()
        for m in AiUsageDashApp.refreshIntervalChoices { popup.addItem(withTitle: "\(m) 分钟") }
        popup.target = self
        popup.action = #selector(freqChanged)
        y = addRow(y, label: "刷新频率", control: popup, view: view, width: 120)
        freqPopup = popup
        // 依赖状态（打开窗口时后台探测）
        y -= 10
        let depTitle = NSTextField(labelWithString: "依赖状态")
        depTitle.font = NSFont.boldSystemFont(ofSize: 12)
        depTitle.frame = NSRect(x: 14, y: y - 4, width: 200, height: 18)
        view.addSubview(depTitle)
        depLabel = NSTextField(labelWithString: "探测中…")
        depLabel.font = NSFont.systemFont(ofSize: 11)
        depLabel.textColor = .secondaryLabelColor
        depLabel.lineBreakMode = .byWordWrapping
        // 紧贴「依赖状态」标题下方（标题占 y-4~y+14），两行文本，不与标题重叠
        depLabel.frame = NSRect(x: 14, y: y - 44, width: view.frame.width - 28, height: 36)
        view.addSubview(depLabel)
        let hint = NSTextField(labelWithString: "更改立即生效；Trae 3.4.x 解密依赖已内嵌于 app。")
        hint.font = NSFont.systemFont(ofSize: 10)
        hint.textColor = .tertiaryLabelColor
        hint.frame = NSRect(x: 14, y: 8, width: 480, height: 14)
        view.addSubview(hint)
        return item
    }

    private func makePushTab(size: NSSize) -> NSTabViewItem {
        let (item, view) = makeTabItem("推送与归档", size: size)
        var y: CGFloat = view.frame.height - 40
        var hostField: NSTextField?
        for f in SettingsStore.pushFields {
            let tf = textField(key: f.key, placeholder: f.placeholder)
            y = addRow(y, label: f.label, control: tf, view: view)
            if f.key == "settings.deviceHost" { hostField = tf }
        }
        let probe = NSButton(title: "测试连接", target: self, action: #selector(probeDevice))
        probe.bezelStyle = .rounded
        probe.frame = NSRect(x: 152, y: y + 6, width: 90, height: 24)
        view.addSubview(probe)
        probeResult = NSTextField(labelWithString: "")
        probeResult.font = NSFont.systemFont(ofSize: 11)
        probeResult.textColor = .secondaryLabelColor
        probeResult.frame = NSRect(x: 252, y: y + 10, width: 260, height: 18)
        view.addSubview(probeResult)
        _ = hostField
        return item
    }

    private func makeAdvancedTab(size: NSSize) -> NSTabViewItem {
        let (item, view) = makeTabItem("高级", size: size)
        var y: CGFloat = view.frame.height - 40
        for f in SettingsStore.advancedFields {
            let tf = textField(key: f.key, placeholder: f.placeholder)
            y = addRow(y, label: f.label, control: tf, view: view, width: 330)
        }
        let hint = NSTextField(labelWithString: "一般无需修改；留空即默认值，与 CLI 环境变量同名同义。")
        hint.font = NSFont.systemFont(ofSize: 10)
        hint.textColor = .tertiaryLabelColor
        hint.frame = NSRect(x: 14, y: y - 2, width: 480, height: 14)
        view.addSubview(hint)
        return item
    }

    // MARK: 状态同步与动作

    private func syncStates() {
        // 登录项
        let status = SMAppService.mainApp.status
        switch status {
        case .enabled:
            loginStatus.stringValue = "已启用"
            loginStatus.textColor = .systemGreen
        case .requiresApproval:
            loginStatus.stringValue = "待批准：请到 系统设置 → 通用 → 登录项 中允许"
            loginStatus.textColor = .systemOrange
        default:
            loginStatus.stringValue = "未启用"
            loginStatus.textColor = .secondaryLabelColor
        }
        loginCheck.state = (status == .enabled) ? .on : .off
        // 刷新频率
        let cur = AiUsageDashApp.refreshIntervalMin
        if let idx = AiUsageDashApp.refreshIntervalChoices.firstIndex(of: cur) {
            freqPopup.selectItem(at: idx)
        }
        // 依赖探测（后台）
        depLabel.stringValue = "探测中…"
        DependencyProbe.probeCrypto { crypto in
            let zstd = DependencyProbe.zstdPath()
            self.depLabel.stringValue =
                "pycryptodome（Trae 解密）：\(crypto ? "✓ 可用" : "✗ 缺失（Trae 3.4.x 无法统计）")\n" +
                "zstd（Codex 归档解压）：\(zstd ?? "未安装（跳过 .jsonl.zst 归档源）")"
        }
    }

    @objc private func toggleLoginItem() {
        do {
            if loginCheck.state == .on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            loginCheck.state = (SMAppService.mainApp.status == .enabled) ? .on : .off
            loginStatus.stringValue = "注册失败：\(error.localizedDescription)"
            loginStatus.textColor = .systemRed
            return
        }
        syncStates()
    }

    @objc private func freqChanged() {
        let idx = freqPopup.indexOfSelectedItem
        guard AiUsageDashApp.refreshIntervalChoices.indices.contains(idx) else { return }
        AiUsageDashApp.setRefreshInterval(AiUsageDashApp.refreshIntervalChoices[idx])   // 持久化+重建定时器+立即刷新
    }

    @objc private func probeDevice() {
        guard let f = fields["settings.deviceHost"] else { return }
        let host = f.stringValue.trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty else {
            probeResult.stringValue = "请先填写设备 IP"
            probeResult.textColor = .secondaryLabelColor
            return
        }
        probeResult.stringValue = "连接中…"
        probeResult.textColor = .secondaryLabelColor
        DependencyProbe.tcpProbe(host: host) { ok in
            self.probeResult.stringValue = ok ? "✓ 可达（80 端口）" : "✗ 不可达"
            self.probeResult.textColor = ok ? .systemGreen : .systemRed
        }
    }

    // MARK: NSTextFieldDelegate（失焦即存；空串移除键 = 恢复默认）

    func controlTextDidEndEditing(_ obj: Notification) {
        guard let tf = obj.object as? NSTextField,
              let key = tf.identifier?.rawValue else { return }
        let v = tf.stringValue.trimmingCharacters(in: .whitespaces)
        if v.isEmpty {
            UserDefaults.standard.removeObject(forKey: key)
        } else {
            UserDefaults.standard.set(v, forKey: key)
        }
    }
}

final class DashMenuController: NSObject, NSMenuDelegate {
    static let shared = DashMenuController()

    /// 全局唯一菜单（状态栏 + 面板右键共用同一实例）：
    /// ⚠️ 必须单例——applyContentMode() 每次切换都会重建内容视图并重挂 menu，
    /// 若每次 buildMenu() 新建实例，cardSubmenu 引用被覆盖、打开中的菜单
    /// 勾选态同步会走错分支（2026-08-31 实测：卡片勾选态不显示）。
    static let menu: NSMenu = {
        let c = shared
        let menu = NSMenu()
        menu.delegate = c
        // 注意：index 1-7 被 menuNeedsUpdate 硬编码引用（1-2 玻璃特效组、3-5 模式项、
        // 6 卡片子菜单、7 更新频率），新增功能项只能加在 freqTitle 之后、separator 之前
        let refresh = NSMenuItem(title: "立即刷新", action: #selector(refreshNow), keyEquivalent: "r")
        refresh.target = c
        let glass = NSMenuItem(title: "液态玻璃效果", action: #selector(toggleGlass), keyEquivalent: "")
        glass.target = c
        let desktop = NSMenuItem(title: "贴桌面模式", action: #selector(toggleDesktopMode), keyEquivalent: "")
        desktop.target = c
        let ct = NSMenuItem(title: "鼠标穿透", action: #selector(toggleClickThrough), keyEquivalent: "")
        ct.target = c
        let locked = NSMenuItem(title: "锁定位置", action: #selector(toggleLocked), keyEquivalent: "")
        locked.target = c
        // 卡片显示子菜单：勾选控制的桌面卡片（顺序固定，随 toolOrder）
        let cardMenu = NSMenu()
        cardMenu.delegate = c
        for key in AiUsageDashApp.toolOrder {
            let mi = NSMenuItem(title: AiUsageDashApp.toolLabels[key] ?? key,
                                action: #selector(toggleCard(_:)), keyEquivalent: "")
            mi.target = c
            mi.representedObject = key
            cardMenu.addItem(mi)
        }
        let cardsTitle = NSMenuItem(title: "卡片显示", action: nil, keyEquivalent: "")
        cardsTitle.submenu = cardMenu
        // 更新频率子菜单：统一管控 Token 统计 + 额度的刷新节拍（默认 5 分钟）
        let freqMenu = NSMenu()
        freqMenu.delegate = c
        for minutes in AiUsageDashApp.refreshIntervalChoices {
            let mi = NSMenuItem(title: String(minutes) + " 分钟",
                                action: #selector(setRefreshIntervalAction(_:)), keyEquivalent: "")
            mi.target = c
            mi.representedObject = minutes
            freqMenu.addItem(mi)
        }
        let freqTitle = NSMenuItem(title: "更新频率", action: nil, keyEquivalent: "")
        freqTitle.submenu = freqMenu
        let transparencyMenu = NSMenu()
        transparencyMenu.delegate = c
        for transparency in GlassTransparency.allCases {
            let item = NSMenuItem(title: transparency.title,
                                  action: #selector(setGlassTransparencyAction(_:)), keyEquivalent: "")
            item.target = c
            item.representedObject = transparency.rawValue
            item.state = transparency == AiUsageDashApp.glassTransparency ? .on : .off
            transparencyMenu.addItem(item)
        }
        let transparencyTitle = NSMenuItem(title: "玻璃透明度", action: nil, keyEquivalent: "")
        transparencyTitle.submenu = transparencyMenu
        // 统计报表子菜单：本地 store（ausd_store）的日报/汇总/区间/排行 + store 更新。
        // 放在 freqTitle 之后、separator 之前（index 8，遵守上方硬编码索引警告）。
        let reportMenu = NSMenu()
        reportMenu.delegate = c
        for (title, action) in [("昨日日报", #selector(DashMenuController.reportDaily)),
                                ("本月汇总", #selector(DashMenuController.reportMonth)),
                                ("近 7 天汇总", #selector(DashMenuController.reportWeek)),
                                ("模型 token 排行", #selector(DashMenuController.reportRank)),
                                ("更新统计 store（昨日）", #selector(DashMenuController.reportStoreUpdate))] {
            let mi = NSMenuItem(title: title, action: action, keyEquivalent: "")
            mi.target = c
            reportMenu.addItem(mi)
        }
        let reportTitle = NSMenuItem(title: "统计报表", action: nil, keyEquivalent: "")
        reportTitle.submenu = reportMenu
        // 局部变量名避开方法名 quit（#selector(quit) 会被局部变量遮蔽报错）
        let quitItem = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = c
        menu.addItem(refresh)
        menu.addItem(glass)
        menu.addItem(transparencyTitle)   // 玻璃特效组：开关+透明度相邻（index 1-2）
        menu.addItem(desktop)
        menu.addItem(ct)
        menu.addItem(locked)
        menu.addItem(cardsTitle)
        menu.addItem(freqTitle)
        menu.addItem(reportTitle)         // 统计报表（index 8）
        let settingsItem = NSMenuItem(title: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = c
        menu.addItem(settingsItem)        // 设置（index 9；menuNeedsUpdate 只引用 1-7）
        menu.addItem(.separator())
        menu.addItem(quitItem)
        return menu
    }()

    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu === DashMenuController.menu {
            // 主菜单：模式勾选态 + 卡片子菜单勾选态 + 频率子菜单勾选态
            menu.items[safe: 1]?.state = AiUsageDashApp.glassMode ? .on : .off
            if let sub = menu.items[safe: 2]?.submenu {
                syncTransparencyStates(sub)   // 玻璃特效组：透明度子菜单
            }
            menu.items[safe: 3]?.state = AiUsageDashApp.desktopMode ? .on : .off
            menu.items[safe: 4]?.state = AiUsageDashApp.clickThrough ? .on : .off
            menu.items[safe: 5]?.state = AiUsageDashApp.locked ? .on : .off
            if let sub = menu.items[safe: 6]?.submenu {
                syncCardStates(sub)
            }
            if let sub = menu.items[safe: 7]?.submenu {
                syncIntervalStates(sub)
            }
        } else if menu === DashMenuController.menu.items[safe: 2]?.submenu {
            syncTransparencyStates(menu)
        } else if menu === DashMenuController.menu.items[safe: 7]?.submenu {
            // 「更新频率」子菜单：勾选当前频率
            syncIntervalStates(menu)
        } else {
            // 「卡片显示」子菜单：只同步各卡的勾选态（打钩 = 显示）
            syncCardStates(menu)
        }
    }

    private func syncIntervalStates(_ submenu: NSMenu) {
        for item in submenu.items {
            guard let minutes = item.representedObject as? Int else { continue }
            item.state = minutes == AiUsageDashApp.refreshIntervalMin ? .on : .off
        }
    }

    private func syncTransparencyStates(_ submenu: NSMenu) {
        for item in submenu.items {
            guard let rawValue = item.representedObject as? String,
                  let transparency = GlassTransparency(rawValue: rawValue) else { continue }
            item.state = transparency == AiUsageDashApp.glassTransparency ? .on : .off
        }
    }

    private func syncCardStates(_ submenu: NSMenu) {
        for item in submenu.items {
            guard let key = item.representedObject as? String else { continue }
            item.state = AiUsageDashApp.visibleTools.contains(key) ? .on : .off
        }
    }

    @objc func refreshNow() { AiUsageDashApp.refresh() }
    @objc func setRefreshIntervalAction(_ sender: NSMenuItem) {
        guard let minutes = sender.representedObject as? Int else { return }
        AiUsageDashApp.setRefreshInterval(minutes)
    }
    @objc func toggleGlass() { AiUsageDashApp.toggleGlass() }
    @objc func setGlassTransparencyAction(_ sender: NSMenuItem) {
        guard let rawValue = sender.representedObject as? String,
              let transparency = GlassTransparency(rawValue: rawValue) else { return }
        AiUsageDashApp.setGlassTransparency(transparency)
        if let submenu = sender.menu {
            syncTransparencyStates(submenu)
        }
    }
    @objc func toggleDesktopMode() { AiUsageDashApp.toggleDesktopMode() }
    @objc func toggleClickThrough() { AiUsageDashApp.toggleClickThrough() }
    @objc func toggleLocked() { AiUsageDashApp.toggleLocked() }
    @objc func toggleCard(_ sender: NSMenuItem) {
        guard let key = sender.representedObject as? String else { return }
        AiUsageDashApp.toggleCardVisibility(key)
    }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func openSettings() { SettingsWindowController.shared.show() }

    // ---- 统计报表（本地 store / 日报脚本，后台执行，文本窗口呈现）----
    @objc func reportDaily() {
        ReportRunner.run(title: "昨日日报",
                         script: "ai_usage_summary.py",
                         args: ["--date", ReportRunner.dateStr(daysAgo: 1)])
    }
    @objc func reportMonth() {
        let month = DateFormatter()
        month.dateFormat = "yyyy-MM"
        ReportRunner.run(title: "本月汇总（store 封闭日）",
                         script: "ausd_store.py",
                         args: ["query", "--month", month.string(from: Date())])
    }
    @objc func reportWeek() {
        ReportRunner.run(title: "近 7 天汇总（store 封闭日）",
                         script: "ausd_store.py",
                         args: ["query", "--from", ReportRunner.dateStr(daysAgo: 7),
                                "--to", ReportRunner.dateStr(daysAgo: 0)])
    }
    @objc func reportRank() {
        ReportRunner.run(title: "模型 token 排行（store）",
                         script: "ausd_store.py", args: ["rank"])
    }
    @objc func reportStoreUpdate() {
        ReportRunner.run(title: "更新统计 store（昨日）",
                         script: "ausd_store.py", args: ["update"])
    }
}

// MARK: - 统计报表窗口与执行器

/// 全局唯一文本报表窗口：monospace 只读，复用不重建（避免多窗口堆积）
final class ReportWindowController {
    static let shared = ReportWindowController()
    private var window: NSWindow?
    private var textView: NSTextView?

    func show(title: String, text: String) {
        if window == nil {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
                               styleMask: [.titled, .closable, .resizable, .miniaturizable],
                               backing: .buffered, defer: false)
            win.title = title
            win.isReleasedWhenClosed = false
            let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 760, height: 560))
            scroll.hasVerticalScroller = true
            scroll.autohidesScrollers = true
            scroll.borderType = .noBorder
            let tv = NSTextView(frame: scroll.bounds)
            tv.isEditable = false
            tv.isRichText = false
            tv.autoresizingMask = [.width]
            tv.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
            tv.textContainerInset = NSSize(width: 12, height: 12)
            scroll.documentView = tv
            win.contentView = scroll
            window = win
            textView = tv
        }
        window?.title = title
        textView?.string = text
        textView?.scroll(.zero)   // 回到顶部
        window?.center()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// 报表命令执行：与 DataFetcher 同一套解释器解析与仓库相对脚本定位
    final class ReportRunner {
        static func scriptPath(_ name: String) -> String {
            BundleLayout.collectorsDir + "/" + name
        }

    static func dateStr(daysAgo: Int) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"   // yyyy=日历年；YYYY 是周年历，年末会错位
        return f.string(from: Date().addingTimeInterval(TimeInterval(-daysAgo) * 86400))
    }

    static func run(title: String, script: String, args: [String]) {
        let runner = DataFetcher.shared
        let scriptPath = scriptPath(script)
        DispatchQueue.global(qos: .userInitiated).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: runner.pythonPath)
            proc.arguments = runner.usesEnvShim
                ? ["python3", scriptPath] + args
                : [scriptPath] + args
            var childEnv = ProcessInfo.processInfo.environment
            childEnv["PYTHONDONTWRITEBYTECODE"] = "1"
            for (k, v) in SettingsStore.envOverrides() { childEnv[k] = v }
            proc.environment = childEnv
            let outPipe = Pipe(), errPipe = Pipe()
            proc.standardOutput = outPipe
            proc.standardError = errPipe
            var timedOut = false
            let timeoutWork = DispatchWorkItem { [weak proc] in
                timedOut = true
                proc?.terminate()
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + 300, execute: timeoutWork)
            var output: String
            do {
                try proc.run()
                let data = outPipe.fileHandleForReading.readDataToEndOfFile()
                let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
                proc.waitUntilExit()
                output = String(data: data, encoding: .utf8) ?? ""
                if timedOut {
                    output += "\n⚠ 超时终止（300s）"
                } else if proc.terminationStatus != 0 {
                    let err = String(data: errData, encoding: .utf8) ?? ""
                    output += "\n⚠ 退出码 \(proc.terminationStatus)" + (err.isEmpty ? "" : "\n" + err)
                }
            } catch {
                output = "⚠ 启动失败: \(error.localizedDescription)\n脚本: \(scriptPath)"
            }
            timeoutWork.cancel()
            let header = "执行: python3 \(scriptPath) \(args.joined(separator: " "))\n"
                         + String(repeating: "─", count: 46) + "\n\n"
            DispatchQueue.main.async {
                ReportWindowController.shared.show(title: title, text: header + output)
            }
        }
    }
}

extension Array {
    /// 越界安全的下标（菜单项状态同步用）
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}

// MARK: - 应用入口

@main
struct AiUsageDashApp {
    static var panel: DashPanel!
    static var statusItem: NSStatusItem!
    static var desktopMode = false
    static var clickThrough = false
    static var locked = false          // 锁定位置（禁止拖动），默认锁定
    static var glassMode = true        // 液态玻璃效果（默认开；--classic 关闭）
    static var glassTransparency: GlassTransparency = .standard
    static let refreshIntervalChoices = [3, 5, 15, 30, 60]   // 更新频率可选值（分钟）
    static var refreshIntervalMin = 5  // 统一更新频率：Token 统计 + 额度全部走这一个节拍
    static var refreshTimer: Timer?

    // —— 卡片可见性（菜单「卡片显示」控制；顺序固定，Set 只管有无）——
    static let toolOrder = ["codex", "claude", "hermes", "trae", "zcode", "workbuddy"]
    static let toolLabels = ["codex": "Codex", "claude": "Claude Code",
                             "hermes": "Hermes", "trae": "Trae", "zcode": "ZCode",
                             "workbuddy": "WorkBuddy"]
    static var visibleTools: Set<String> = Set(["codex", "claude", "hermes", "trae", "zcode", "workbuddy"])

    /// 当前内容视图（二选一，玻璃/经典）
    static var glassContent: GlassDashContent?
    static var classicView: DashView?

    /// 当前展示的数据缓存：事件增量合并的基础（仅主线程访问）
    static var dataCache: DashData?
    /// 最近一次成功采集所属的本地日（yyyy-MM-dd）：跨天检测用。
    /// 跨 0 点后单工具事件刷新若仍用昨日缓存合并，其它卡会挂着昨日全天累计直到下个定时 tick。
    static var dataDay: String?
    /// 文件系统事件监控（6 个工具的日志目录）
    static let fsMonitor = FSEventMonitor()

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)     // 无 Dock 图标

        // 状态栏图标：clickthrough 时窗口不接收鼠标事件（含右键），
        // 状态栏菜单是唯一的常驻控制入口
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "gauge", accessibilityDescription: "AI 用量仪表盘")
        }
        statusItem.menu = DashMenuController.menu

        // 模式：默认贴桌面（用户诉求：不挡全屏 App）；--floating 可显式切回置顶
        let args = CommandLine.arguments
        let defaults = UserDefaults.standard
        if args.contains("--floating") { desktopMode = false }
        else if args.contains("--desktop") { desktopMode = true }
        else {
            desktopMode = defaults.object(forKey: "desktopMode") == nil
                ? true                                     // 首次运行默认贴桌面
                : defaults.bool(forKey: "desktopMode")
        }
        if args.contains("--clickthrough") { clickThrough = true }
        else { clickThrough = defaults.bool(forKey: "clickThrough") }
        // 锁定位置：默认锁定（首次运行无记录即锁定）；--movable 显式解锁
        if args.contains("--movable") { locked = false }
        else if args.contains("--locked") { locked = true }
        else {
            locked = defaults.object(forKey: "locked") == nil
                ? true
                : defaults.bool(forKey: "locked")
        }
        // 液态玻璃：默认开启；--classic 显式关闭；菜单可随时切换
        if args.contains("--classic") { glassMode = false }
        else if args.contains("--glass") { glassMode = true }
        else {
            glassMode = defaults.object(forKey: "glassMode") == nil
                ? true
                : defaults.bool(forKey: "glassMode")
        }
        glassTransparency = GlassTransparency(rawValue: defaults.string(forKey: "glassTransparency") ?? "")
            ?? .standard
        // 统一更新频率：从 UserDefaults 恢复（默认 5；只接受合法值）
        if let savedMin = defaults.object(forKey: "refreshIntervalMin") as? Int,
           refreshIntervalChoices.contains(savedMin) {
            refreshIntervalMin = savedMin
        }
        // 卡片可见性：从 UserDefaults 恢复（默认全显示）；只保留合法 key，至少一张
        if let saved = defaults.array(forKey: "visibleTools") as? [String] {
            let valid = Set(saved).intersection(Set(toolOrder))
            if !valid.isEmpty {
                visibleTools = valid
            }
        }

        panel = DashPanel()
        panel.level = desktopMode ? .desktopPinnedLevel : .floating
        panel.ignoresMouseEvents = clickThrough
        panel.isMovableByWindowBackground = !clickThrough
        applyContentMode()

        // 探针：档位自动切换（A/B/A 同进程泄漏测试用），GLASS_TIER_SCHEDULE="clear:10,standard:22"
        if let sched = ProcessInfo.processInfo.environment["GLASS_TIER_SCHEDULE"] {
            for pair in sched.split(separator: ",") {
                let kv = pair.split(separator: ":")
                if kv.count == 2, let tier = GlassTransparency(rawValue: String(kv[0])),
                   let delay = Double(kv[1]) {
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                        AiUsageDashApp.setGlassTransparency(tier)   // 与菜单同一生产路径
                        dashLog("tier-schedule -> \(tier.rawValue) @\(Int(delay))s")
                    }
                }
            }
        }

        // 位置记忆：恢复上次 frame 的【位置】，尺寸强制用当前布局（避免旧尺寸残留裁剪）
        if let frameStr = defaults.string(forKey: "panelFrame"),
           let rect = NSRectFromString(frameStr) as NSRect?,
           rect.width > 0, rect.height > 0 {
            var newRect = rect
            newRect.size = NSSize(width: Layout.w,
                                  height: Layout.windowH(cards: visibleTools.count))
            panel.setFrame(newRect, display: false)
        } else {
            positionTopRight()
        }
        NotificationCenter.default.addObserver(
            forName: NSWindow.didMoveNotification, object: panel, queue: .main) { _ in
            defaults.set(NSStringFromRect(panel.frame), forKey: "panelFrame")
        }
        panel.orderFrontRegardless()

        refresh()   // 首次启动全量
        // 统一更新频率定时器：用户可选 3/5/15/30/60min（默认 5），Token 统计 + 额度同节拍
        //（collect 全量已含三家额度，单通道全覆盖；事件驱动见下方 fsMonitor）
        scheduleRefreshTimer()
        // 事件驱动主通道：6 个工具的日志目录监控，秒级实时
        fsMonitor.start { tools in
            // FSEvent 回调在 monitor 队列：hop 回主线程（queuedModes/dataCache 仅主线程访问）
            DispatchQueue.main.async {
            dashLog("handler tools=\(tools)")
            if tools.count == 1, let t = tools.first {
                refresh(tool: t)    // 单工具变化 → 只刷该工具
            } else {
                refreshStatsOnly()  // 多工具同时变化 → 全量但不带额度（额度严格按设置频率）
            }
            }   // DispatchQueue.main.async（FSEvent 回调 hop 回主线程）
        }

        app.run()
    }

    /// 按当前 glassMode 重建内容视图（玻璃 ↔ 经典随时切换）
    static func applyContentMode() {
        if glassMode {
            let content = GlassDashContent()
            let container = NSGlassEffectContainerView(frame: content.frame)
            container.spacing = 0   // 0 = 仅批处理渲染，不合并相邻玻璃（保持卡片独立）
            container.contentView = content
            container.menu = DashMenuController.menu
            panel.contentView = container
            glassContent = content
            classicView = nil
            content.update(dataCache)
        } else {
            let v = DashView()
            v.menu = DashMenuController.menu
            panel.contentView = v
            v.data = dataCache
            classicView = v
            glassContent = nil
        }
        panel.contentView?.needsDisplay = true
    }

    /// 数据 → 当前内容视图
    static func pushData() {
        if glassMode { glassContent?.update(dataCache) }
        else { classicView?.data = dataCache }
    }

    /// 右上角（避让菜单栏，屏幕顶部留 40pt）
    static func positionTopRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(x: vf.maxX - size.width - 20,
                                     y: vf.maxY - size.height - 8))
    }

    /// 全量刷新：Token 统计 + 三家额度（额度 GET 随此路径出网）
    static func refresh() {
        dashLog("refresh() 全量")
        DataFetcher.shared.fetch { delta in
            dashLog("refresh() 完成 ok=\(delta.ok)")
            let base = dataCache ?? DashData(codex: nil, claude: nil, hermes: nil, trae: nil, zcode: nil,
                                             workbuddy: nil,
                                             total: nil, time: nil, ok: false,
                                             error: nil, fetch_ts: nil)
            dataCache = base.merged(with: delta)
            if delta.ok { dataDay = currentDay() }
            pushData()
        }
    }

    /// 全量但不带额度（事件驱动多工具变化用，额度严格按设置频率）
    static func refreshStatsOnly() {
        dashLog("refreshStatsOnly()")
        DataFetcher.shared.fetchStatsOnly { delta in
            dashLog("refreshStatsOnly() 完成 ok=\(delta.ok)")
            guard let cache = dataCache else { return }
            dataCache = cache.merged(with: delta)
            if delta.ok { dataDay = currentDay() }
            pushData()
        }
    }

    /// 单工具事件刷新：只合并该工具卡（delta 其余字段 null 自动跳过）
    static func refresh(tool: String) {
        // 跨天检测：缓存还是昨日的数据时，单工具增量会把其它卡留在昨日全天累计，
        // 且刷新「更新时间」造成数据新鲜的假象 → 升级为全量 stats 刷新（零出网，额度仍按定时节拍）。
        // 升级失败（ok=false）时 dataDay 不翻新，下一笔事件继续升级重试。
        if let d = dataDay, d != currentDay() {
            dashLog("跨天检测 dataDay=\(d) ≠ 今日 → 单工具刷新升级为全量 stats")
            refreshStatsOnly()
            return
        }
        dashLog("refresh(tool:\(tool))")
        DataFetcher.shared.fetch(tool: tool) { delta in
            dashLog("refresh(tool:\(tool)) 完成 ok=\(delta.ok)")
            guard delta.ok, let cache = dataCache else { return }
            dataCache = cache.merged(with: delta)
            dataDay = currentDay()
            pushData()
        }
    }

    /// 本地日界串（yyyy-MM-dd）：跨天检测用
    static func currentDay() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    /// 统一更新频率：重建定时器（启动/设置变更时调用；collect 全量已含额度）
    static func scheduleRefreshTimer() {
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(
            withTimeInterval: TimeInterval(refreshIntervalMin * 60), repeats: true) { _ in
            refresh()
        }
    }

    /// 菜单设置更新频率：持久化 + 立即按新频率刷一次
    static func setRefreshInterval(_ minutes: Int) {
        guard refreshIntervalChoices.contains(minutes), minutes != refreshIntervalMin else { return }
        refreshIntervalMin = minutes
        UserDefaults.standard.set(minutes, forKey: "refreshIntervalMin")
        scheduleRefreshTimer()
        refresh()
        dashLog("refreshIntervalMin=" + String(minutes))
    }

    static func setGlassTransparency(_ transparency: GlassTransparency) {
        guard transparency != glassTransparency else { return }
        glassTransparency = transparency
        UserDefaults.standard.set(transparency.rawValue, forKey: "glassTransparency")
        if glassMode {
            let frame = panel.frame
            applyContentMode()
            panel.setFrame(frame, display: true)
        }
    }

    static func toggleGlass() {
        glassMode.toggle()
        UserDefaults.standard.set(glassMode, forKey: "glassMode")
        dashLog("glassMode=\(glassMode)")
        applyContentMode()
    }

    static func toggleDesktopMode() {
        desktopMode.toggle()
        panel.level = desktopMode ? .desktopPinnedLevel : .floating
        UserDefaults.standard.set(desktopMode, forKey: "desktopMode")
    }

    static func toggleClickThrough() {
        clickThrough.toggle()
        panel.ignoresMouseEvents = clickThrough
        panel.isMovableByWindowBackground = !clickThrough && !locked   // 穿透或锁定时不能拖动
        UserDefaults.standard.set(clickThrough, forKey: "clickThrough")
    }

    static func toggleLocked() {
        locked.toggle()
        panel.isMovable = !locked
        panel.isMovableByWindowBackground = !locked && !clickThrough
        UserDefaults.standard.set(locked, forKey: "locked")
    }

    /// 切换一张卡的可见性 → 即时隐藏/显示 + 窗口高度自适应（不重建窗口内容）
    static func toggleCardVisibility(_ key: String) {
        if visibleTools.contains(key) {
            guard visibleTools.count > 1 else { return }   // 至少保留一张卡
            visibleTools.remove(key)
        } else {
            visibleTools.insert(key)
        }
        UserDefaults.standard.set(Array(visibleTools).sorted(), forKey: "visibleTools")
        if glassMode, let g = glassContent {
            g.relayout()                                    // 玻璃：隐藏/显示 + 重排
        } else {
            classicView?.needsDisplay = true                // 经典：自绘重绘即过滤
        }
        var f = panel.frame
        f.size = NSSize(width: Layout.w, height: Layout.windowH(cards: visibleTools.count))
        panel.setFrame(f, display: true)                    // 高度自适应，位置不变
        dashLog("visibleTools=\(visibleTools.sorted()) size=\(Int(f.size.height))")
    }
}
