// fig: {"content": "explainer", "style": "warm-sketch", "candidates": ["cool-clean", "warm-sketch", "rough-whiteboard", "im-marker"], "why": "主循环和升级梯需要手工摆位，避免连线绕行和交叉", "surface": "repo-doc", "seed": 1}
const FS = 21;
function mbox(x, y, w, h, lines) {
  box(x, y, w, h, '', { size: FS });
  T.lastChild.remove();
  const top = y + (h - lines.length * FS * 1.25) / 2;
  txt(x, top, lines.join('<br>'), FS, C.ink, w, true);
}
function go(x1, y1, x2, y2) {
  const k = { stroke: C.gray };
  svg.appendChild(rc.line(x1, y1, x2, y2, stroke(k)));
  const a = Math.atan2(y2 - y1, x2 - x1), L = 12;
  svg.appendChild(rc.line(x2, y2, x2 - L * Math.cos(a - 0.4), y2 - L * Math.sin(a - 0.4), stroke(k)));
  svg.appendChild(rc.line(x2, y2, x2 - L * Math.cos(a + 0.4), y2 - L * Math.sin(a + 0.4), stroke(k)));
}
function label(x, y, s, width, center, color) {
  txt(x, y, s, FS, color || C.dim, width, center);
}

title('头脑风暴工作流', 31);

// 准备：定题后，材料和基线并行
mbox(140, 84, 400, 56, ['1 记下原始问题、硬约束和你的想法']);
go(180, 140, 180, 170);
go(490, 140, 490, 170);
mbox(30, 170, 300, 86, ['2 互盲选角度，收集材料', '切成带出处的卡片']);
mbox(412, 170, 238, 86, ['3 不给方法跑 3 次', '得到常见点子清单']);

go(60, 256, 60, 292);
label(72, 264, '每个 agent 随机分一部分', 258);
go(490, 256, 490, 300);
label(502, 264, '用来标记撞车', 148);

// 左列：下一批怎么想，从上到下逐级升级
box(18, 292, 324, 712, '', { size: FS });
T.lastChild.remove();
panel(30, 300, 300, 248, 'new');
label(30, 308, '4 常规发散', 300, true, C.greenInk);
mbox(40, 342, 280, 100, ['第一轮：互盲，不拆解', '脚本分配模型、方法、材料', '列标题→求异→展开']);
go(180, 442, 180, 464);
mbox(40, 464, 280, 72, ['第二轮：按拆法和空白方向', '指派，带禁区']);

go(52, 548, 52, 584);
label(66, 553, '枯竭：原创区从这里开始', 264, false, C.redInk);

panel(30, 584, 300, 408, 'old');
label(30, 592, '4 压力升级', 300, true, C.redInk);
mbox(40, 626, 280, 72, ['压力一 全禁区', '已有机制全部禁用']);
go(180, 698, 180, 720);
mbox(40, 720, 280, 72, ['压力二 换燃料', '冷门卡片、远领域']);
go(180, 792, 180, 814);
mbox(40, 814, 280, 72, ['压力三 攻击前提', '反转点子扎堆处的假设']);
go(180, 886, 180, 908);
mbox(40, 908, 280, 72, ['压力四 换方法', '加极端约束']);

// 右列：每一批都走同一个判断
go(342, 346, 412, 346);
label(342, 316, '产出', 70, true);
mbox(412, 300, 238, 92, ['5 按机制去重', '撞车的退回一次']);
go(490, 392, 490, 430);
mbox(412, 430, 238, 66, ['这一批有新机制吗？']);
go(412, 463, 342, 463);
label(344, 470, '下一批', 66, true);

label(412, 512, '有：本级再跑一批<br>没有：升一级', 210, false, C.ink);
label(412, 590, '下一批带上：<br>已有机制（禁区）<br>拆法汇总<br>只出现一次的点子', 210, false, C.dim);

// 退出
go(640, 496, 640, 1040);
txt(400, 952, '压力四也没有新机制<br>或预算用完', FS, C.ink, 228, false);
T.lastChild.style.textAlign = 'right';

// 收敛
mbox(30, 1040, 620, 56, ['6 组合：跨子问题拼成完整方案']);
go(340, 1096, 340, 1124);
mbox(30, 1124, 620, 86, ['7 新评委排序：打乱顺序、隐去来源', '分项加总分，只排序，不删点子']);
go(340, 1210, 340, 1238);
mbox(30, 1238, 620, 86, ['8 你看完全部点子，挑中的出试错方案', '不自动执行，由人拍板']);
watermark();
