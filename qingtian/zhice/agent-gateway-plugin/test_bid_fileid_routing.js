// 二十三轮（2026-08-28 小智 2c52850b stdout 实锤）：文件ID形态路由回归。
// 根因：_isBidCommand 的 文件id[:：]（i 不区分大小写）把「文件ID：xxx。批量整理
// 技术规范」判成 bidding → before_dispatch 硬路径 generate_bid 抢走 bid_prep。
// 修复：_bidFileIdOnly（判线 bidding 且无投标词）→ probe 消歧；probe 守卫加 bid_prep。
// 纯函数直连 require（index.js require 无副作用），零依赖 node assert。
const assert = require('assert');
const plugin = require('./index.js');

const UUID = '393a1f95-dd36-409f-a928-a0ca78ea1052';
let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n++; };

// ── ① 文件ID形态（bid_prep 需求，生产实测原文）→ 消歧 ──
const prod = `文件ID：${UUID}。批量整理技术规范，按zip文件里的顺序`;
ok(plugin._bidFileIdOnly(prod) === true, '生产原文：文件ID+技术规范 → 消歧');
ok(plugin._businessLine(prod) === 'bidding', '判线仍为 bidding（消歧在 dispatch 层，闸不动）');
ok(plugin._bidFileIdOnly(`文件标识：${UUID}，整理一下`) === true, '文件标识形态 → 消歧');
// 网关 _isBidCommand 只认「文件id[:：]/文件标识[:：]」（中文+冒号紧随）——英文 file_id
// 与无冒号空格形态网关本就不拦（→ message_received probe 链，tags 层已能正确路由
// bid_prep），不在本次误伤范围，保持不动：
ok(plugin._bidFileIdOnly(`file_id: ${UUID}`) === false, '英文形态网关不拦（走 probe 链，本就正确）');
ok(plugin._bidFileIdOnly(`文件ID ${UUID}`) === false, '无冒号空格形态网关不拦（同上）');

// ── ② 显式投标词 → 硬路径不动 ──
ok(plugin._bidFileIdOnly(`生成标书 文件ID：${UUID}`) === false, '生成标书+文件ID → 硬路径');
ok(plugin._bidFileIdOnly('生成标书') === false, '生成标书 → 硬路径');
ok(plugin._bidFileIdOnly(`投标文件用这个 文件ID：${UUID}`) === false, '含投标词 → 硬路径');
// 遗留语义保持：投标词+技术规范同现仍走硬路径（fix-log 已记，待样本再评估）
ok(plugin._bidFileIdOnly('整理招标文件里的技术规范书') === false, '招标词同现 → 硬路径（遗留语义不变）');

// ── ③ 非投标消息 → 不受影响 ──
ok(plugin._bidFileIdOnly('你好') === false, '闲聊 → 不动');
ok(plugin._bidFileIdOnly('帮我看下这个报价') === false, '采购线 → 不动');
ok(plugin._bidFileIdOnly(`见附件 ${UUID} 的说明`) === false, '裸 UUID 无引导词 → 不判（bid_prep 正则同约定）');
ok(plugin._bidFileIdOnly('') === false, '空文本防御');

// ── ④ 判线回归：拆分后 _businessLine 各线结果不变 ──
ok(plugin._businessLine('生成标书') === 'bidding', 'businessLine bidding 不变');
ok(plugin._businessLine('询价一批电缆') === 'procurement', 'businessLine procurement 不变');
ok(plugin._businessLine('今天天气不错') === '', 'businessLine 放行不变');

console.log(`test_bid_fileid_routing: ${n} passed`);
