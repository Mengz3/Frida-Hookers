/*
本质上不是去硬解壳算法，也不是静态修复壳包，而是：
等壳自己把 dex 解密并交给 Android 运行时使用时，趁机把内存里的 dex 直接拷出来。

这个脚本是 Android_Dump_Dex 项目的一个典型运行时脱壳脚本，整体思路分三步：
1. 先 hook fork()，尽量避免目标进程通过创建子进程干扰 Frida。
2. 再监控 android_dlopen_ext，粗略识别常见加固 so 是否被加载。
3. 最后 hook libart 中的 ClassLinker::DefineClass，在运行时类定义阶段抓取 DexFile 内存并落盘。

它更适合处理“运行时解密/动态加载 dex”的场景，而不是静态修复壳。
*/


/*
Hook fork 的目的是防止目标进程通过创建子进程干扰 Frida。
这里统一让 fork 返回 -1，并把 errno 伪装成 EPERM。
*/
(() => {
    const forkSymbol = Module.findGlobalExportByName("fork");
    if (!forkSymbol) {
        console.warn("[-] fork() not found");
        return;
    }

    // 获取 errno 地址，后续在伪造 fork 失败时顺手写入 errno。
    const errnoPtr = (() => {
        const errnoLocation = Module.findGlobalExportByName("__errno_location");
        return errnoLocation ? new NativeFunction(errnoLocation, "pointer", [])() : null;
    })();

    // 将 fork() 直接替换成“返回 -1”的安全版本，减少子进程分叉导致的注入干扰。
    const safeForkHandler = new NativeCallback(() => {
        console.warn("[!] Fork intercepted - returning -1 (EPERM)");
        if (errnoPtr) errnoPtr.writeS32(1);
        return -1;
    }, 'int', []);

    Interceptor.replace(forkSymbol, safeForkHandler);
    console.warn("[+] Fork hook: ACTIVE");
})();

/* 内置模板默认包名，创建工作区时会被替换成当前目标 App 包名 */
const TARGET_PKG = "com.smile.gifmaker";
const SAFE_DIR = `/data/data/${TARGET_PKG}/`;

// 常见加固 so 的名字特征，用于在 dlopen 阶段做“加固类型提示”。
const DETECTION_LIBRARIES = [
    { pattern: "libdexprotector", message: "DexProtector: https://licelus.com" },
    { pattern: "libjiagu", message: "Jiagu360: https://jiagu.360.cn" },
    { pattern: "libAppGuard", message: "AppGuard: http://appguard.nprotect.com" },
    { pattern: "libDexHelper", message: "Secneo: http://www.secneo.com" },
    { pattern: "libsecexe|libsecmain|libSecShell", message: "Bangcle: https://github.com/woxihuannisja/Bangcle" },
    { pattern: "libprotectt|libapp-protectt", message: "Protectt: https://www.protectt.ai" },
    { pattern: "libkonyjsvm", message: "Kony: http://www.kony.com/" },
    { pattern: "libnesec", message: "Yidun: https://dun.163.com/product/app-protect" },
    { pattern: "libcovault", message: "AppSealing: https://www.appsealing.com/" },
    { pattern: "libpairipcore", message: "Pairip: https://github.com/rednaga/APKiD/issues/329" }
];

function hookDlopen() {
    return new Promise((resolve, reject) => {
        try {
            const isArm = Process.arch === "arm" ? "linker" : "linker64";
            const reg = Process.arch === "arm" ? "r0" : "x0";
            const linker = Process.findModuleByName(isArm);
            if (!linker) {
                reject(new Error("未找到 linker 模块"));
                return;
            }
            let resolved = false;
            const resolveOnce = () => {
                if (!resolved) {
                    resolved = true;
                    resolve();
                }
            };
            // 通过 linker 导出的 android_dlopen_ext 观察运行时加载的 so。
            const sym = linker.enumerateExports().find(e => e.name.includes('android_dlopen_ext'));
            Interceptor.attach(sym.address, {
                onEnter(args) {
                    const libPath = this.context[reg].readUtf8String();
                    if (!libPath) return;
                    // 一旦命中常见加固 so，就提示对应壳类型并尽早进入 dump 阶段。
                    for (const { pattern, message } of DETECTION_LIBRARIES) {
                        if (new RegExp(pattern).test(libPath)) {
                            console.warn(`\n[*] Packer Detected: ${message}`);
                            resolveOnce();
                            return;
                        }
                    }
                }
            });
            // 如果几秒内没检测到任何典型加固 so，也继续后续流程，避免一直卡住。
            setTimeout(resolveOnce, 3000);
        } catch (e) {
            reject(new Error("Unsupported architecture/emulator"));
        }
    });
}

function processDex(Buf, C, Path) {
    // 确保读到的内存块长度看起来至少像一个 dex 头。
    if (!Buf || Buf.byteLength < 8) {
        console.error(`[!] Invalid buffer for classes${C - 1}.dex`);
        return;
    }
    const DumpDex = Buf instanceof Uint8Array ? Buf : new Uint8Array(Buf);
    const Count = C - 1;

    // 几种简单的头部特征检测：
    // 1. CDEX：紧凑 dex，一般先忽略
    // 2. Empty Header：前面全 0，常见于某些壳
    // 3. Wiped Header：头部被清空或篡改，但内存块依然值得保存
    const CDEX_SIGNATURE = [0x63, 0x64, 0x65, 0x78, 0x30, 0x30, 0x31];
    const EMPTY_HEADER = [0x00, 0x00, 0x00, 0x00];
    const WIPED_HEADER = [0x64];

    // 检测 CDEX
    if (CDEX_SIGNATURE.every((val, i) => DumpDex[i] == val)) {
        console.warn(`[*] classes${Count}.dex is a Compact Dex (CDEX). Ignoring.`);
        return;
    }

    // 检测空头 dex（常见于 DexProtector 一类场景）
    if (EMPTY_HEADER.every((val, i) => DumpDex[i] == val) && DumpDex[7] == 0x00) {
        console.warn(`[*] 00000 Header detected in classes${Count}.dex, possible DexProtector.`);
        writeDexFile(Count, Buf, Path, 0);
        return;
    }

    // 检测被抹掉/篡改头部的 dex
    if (DumpDex[0] == 0x00 || WIPED_HEADER.every((val, i) => DumpDex[i] != val)) {
        console.warn(`[*] Wiped Header detected, classes${Count}.dex might be interesting.`);
        writeDexFile(Count, Buf, Path, 0);
        return;
    }

    // 默认按普通 dex 处理
    writeDexFile(Count, Buf, Path, 1);
}

function writeDexFile(count, buffer, path, isValid) {
    try {
        const file = new File(path, "wb");
        file.write(buffer);
        file.close();
        console.log(`[Dex${count}] Saved to: ${path} ${isValid ? '(valid)' : '(modified)'}`);
    } catch (error) {
        console.error(`[!] Failed to save Dex${count} to ${path}: ${error.message}`);
    }
}

function findDefineClass(libart) {
    // 不同 Android 版本里符号位置不一定统一，所以 symbols/imports/exports 都尝试一遍。
    const matcher = /ClassLinker.*DefineClass.*Thread.*DexFile/;
    const search = (items, type) => items.find(item => matcher.test(item.name))?.address;

    return search(libart.enumerateSymbols(), 'symbols') ||
           search(libart.enumerateImports(), 'imports') ||
           search(libart.enumerateExports(), 'exports');
}

function dumpDex() {
    const libart = Process.findModuleByName("libart.so");
    if (!libart) return console.error("[!] 未找到 libart.so");

    const defineClassAddr = findDefineClass(libart);
    console.warn("[*] DefineClass found at : ", defineClassAddr);
    if (!defineClassAddr) return console.error("[!] 未找到 DefineClass");

    const seenDex = new Set();
    let dexCount = 1;

    // 在 ClassLinker::DefineClass 时机抓取 DexFile：
    // args[5] 通常对应 DexFile*，其 begin_ 和 size_ 字段可用于直接读取整块 dex 内存。
    Interceptor.attach(defineClassAddr, {
        onEnter(args) {
            const dexFilePtr = args[5];
            const base = dexFilePtr.add(Process.pointerSize).readPointer();
            const size = dexFilePtr.add(Process.pointerSize * 2).readUInt();

            // 以 dex 基址去重，避免同一块 dex 被重复落盘。
            if (seenDex.has(base.toString())) return;
            seenDex.add(base.toString());

            const dexBuffer = base.readByteArray(size);
            if (!dexBuffer || dexBuffer.byteLength !== size) return;

            const path = `${SAFE_DIR}classes${dexCount}.dex`;
            processDex(dexBuffer, dexCount++, path);
        }
    });
}

async function main() {
    try {
        await hookDlopen();
        console.warn("[*] Hook 完成，开始导出 dex...");
        dumpDex();
    } catch (e) {
        console.error(`[!] Error: ${e.message}`);
    }
}

// 脚本加载后立即执行主流程。
setImmediate(main);
