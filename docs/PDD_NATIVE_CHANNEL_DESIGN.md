# PDD 原生通道方案（基于 3.5.0.40 导出表实测）

> 结论先行：**不要虚表嗅探、不要猜参数**。`MsgSdk.dll` 把 `CMChatImpl` 的方法
> **按名字导出**（83 个），签名就写在 mangled name 里。按名字 `GetProcAddress`
> 拿到地址，按**已知签名**调用/挂钩，跨版本稳定，且不会读错内存。

## 一、为什么之前会崩（证据）

| 做法 | 结果 |
|---|---|
| vtable 32 槽全嗅探（`RECV_AUTO`） | `PddWorkbench` 崩溃（22:10 dump，`0xC0000005`）|
| 导出 detour 按 `string*` 读 6 个参数 | 22:17 再次崩溃 |
| 原因 | 每个槽/每个函数的真实参数类型不同，按错类型解引用就是访问违例 |

对照：
- **探域**：每个客户端版本一个插件 DLL + 一份客户端副本（`platformSoftware/pdd/3.5.0.40_PddWorkbench.exe`），
  hook 点硬编码并按版本验证（`HookInterceptMainFrameMsg` 等 20 个），**不嗅探**。
- **福客**：MinHook + 符号名定位（`websocket::ISocket::StringToJson`、
  `message::MyMBusinessMgrNotifyPushSyncMsg`），**按已知签名挂钩**。

## 二、可用的导出（3.5.0.40 实测，共 83 个 CMChatImpl 方法）

### 入站：消息落库入口（首选 hook 点）

```
?InsertMsg@CMChatImpl@@UEAA_NAEBV<string>@@0AEBVValue@Json@@@Z
→ bool InsertMsg(this, const std::string& a1, const std::string& a2, const Json::Value& msg)
```

- 前两个参数类型明确是 `const std::string&`，可以**安全读取**（我们已有 `msabi::MsvcString` 读取器，
  含 SSO 短串处理；读前做 size/IsBadReadPtr 校验）。
- 第一个参数 `this` 就是 **`CMChatImpl` 实例** —— 发送也需要它。
- 第三个参数是 Json::Value，**不要解析**（布局依赖 jsoncpp 版本），只当作"有新消息"的信号。

备选：`NotifyWSMessage`（实测只有心跳）、`CheckWSMsg`、`SyncMessage`。

### 出站：发送

```
?SendTextMsg@CMChatImpl@@...      (15 参数，已跑通)
?SendImageMsg@CMChatImpl@@...     (4×string + wstring + string + 3×int)
?SendImage@CMChatImpl@@...        (string + string + int + string + wstring + string)
?SendCardMsg / SendDuoDuoJi / SendFileCardToService
```

关键：`csid` 必须是**页面客服身份**（`userInfo.username`，实测为「主账号」），
传空或传 `cs_mall:uid` 一律返回 false。

### 查询：不依赖页面 socket（重要）

```
GetChatRecord / LatestUserChatRecord / LatestServiceChatRecord
QueryUserChatRecord / PullChatRecord / PullChatRecordToId / BatchGetChatRecord
GetAllListsConversations / GetServiceConversations / GetPlatformConversations
```

这批方法**直接在进程内调用**即可，不需要"打开会话"、不需要页面 `socketUtil`。
这意味着回扫可以彻底摆脱"页面 socket 必须活着"的限制。

## 三、目标架构

```
PddWorkbench 进程
├─ MsgSdk.dll
│   └─ CMChatImpl::InsertMsg(this, a1, a2, json)
│        ▲ 单点 inline hook（签名已知，只读 2 个 string）
│        │
│        ├─ 记录 this → CMCHAT 实例（发送用）
│        └─ 发出"新消息"信号（账号/买家）
│
└─ 自有 DLL ── 命名管道 ── 桥接
                              └─ 收到信号 → 调 GetChatRecord 取完整消息 → 上报中心
```

优点：
- 只 hook **一个**函数、且参数类型确定 → 不会像全槽位嗅探那样随机崩；
- 实例从 `InsertMsg` 的 `this` 拿，不依赖构造函数时机；
- 回扫改成 `GetChatRecord`，不再依赖页面会话是否打开。

## 四、实现要点（安全约束）

1. 定位：`GetProcAddress("MsgSdk.dll", "?InsertMsg@CMChatImpl@@UEAA_NAEBV<string>@@0AEBVValue@Json@@@Z")`
   —— **按导出名**，不扫 RVA、不扫特征码。
2. 挂钩：用现有 `inline_hook.h`（序言解码 + fail-closed，认不出就不挂）。
3. 参数读取：只读 a1/a2，且
   - `IsBadReadPtr` 判可读；
   - `size == 0 || size > 1MB` 直接放弃；
   - SSO 短串（size < 16）从 `bx.buf` 读，长串从 `bx.ptr` 读。
4. `Json::Value` **不读**。
5. 钩子里禁止 IO / JSON 解析 / 锁 —— 只做一次定长拷贝 + 入队，解析放到自线程。
6. 发送：`SendTextMsg/SendImageMsg(instance, csid="主账号", buyer, ...)`。
7. 全部默认关闭，`native_channel=insert_msg` 才启用；启用前先在**副本客户端**验证。

## 五、验证步骤（不碰生产客户端）

1. 用 `D:\kefuAgent\探域\tanyu3.0.2\platformSoftware\pdd\3.5.0.40_PddWorkbench.exe`
   作为副本客户端（版本一致，独立目录）；
2. 在副本里登录测试账号 → 挂 `InsertMsg` hook → 让买家发一条；
3. 断言：hook 触发一次、a1/a2 可读、`this` 非 0、连续 30 分钟不崩；
4. 通过后再推到 90 科技客户机，先单店灰度。

## 六、现状（0.7.20）

```
recv_via_dll = false      ← 原生接收关闭（vtable/detour 都会崩）
send_via_dll = false      ← 原生发送关闭（依赖实例，实例靠 hook）
data_source  = cdp        ← 回到"页面 + 回扫"的稳定行为
```

已修且保留的安全项：csid 统一口径、`OK 0` 回退页面、倒计时联动、安装前彻底退旧进程、
新鲜补拉提升为实时、回扫重复上报修复。
