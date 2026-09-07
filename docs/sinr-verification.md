# QDC507 SINR 实机核实

2026-09-07 UTC，通过部署实例已有的认证 AT API 查询；服务未停止，未修改模块配置。

- ATI：Baiwang / QDC507 / Revision: QDC507GLEFM21
- AT+GMR：QDC507GLEFM21
- QENG 与 QCSQ 均返回 OK。

一组相邻查询：

```text
+QCSQ: "LTE",85,-124,55,-18
+QENG: "servingcell","NOCONN","LTE","FDD",302,220,188E598,428,66935,66,5,5,2CEE,-122,-19,-82,-9,5
```

QCSQ 的 SINR 为第三个数值 55，按 raw / 5 - 20 换算为 -9 dB。
QENG 的 SINR 位于倒数第二个字段，直接为 -9 dB。
另一组 QCSQ=68 换算 -6.4 dB，相邻 QENG=-6。
六次 QENG 查询中 SINR 为 -6、-7、-6、-9、-4、-9。

这些响应支持本固件 QENG 直接返回 dB 的解释，因此 API 的
radio_metrics.sinr_db 直接采用 QENG 的该字段，同时保留 sinr_raw。
两条命令不是同一时刻采样，实测并非每一组都相等，不能要求逐组严格对应。
该结论限于已核对的固件与 18 字段 LTE 格式；其他格式仍按不支持处理。

QCSQ 的 0–250 编码与 -20 至 +30 dB 的映射可参照
[移远 QCSQ 文档](https://quectel.com/content/uploads/2021/03/Quectel_BG96_AT_Commands_Manual_V2.3.pdf)。
该文档属于其他型号，作为编码交叉参考，并非 QDC507 的型号专用规格。

同时观察到 QCSQ 的 RSSI 返回正数 85，QENG 的 RSSI 返回 -81 至 -84。
当前 API 继续使用 QENG 的 RSSI，不对 QCSQ RSSI 的单位或符号做推断。
