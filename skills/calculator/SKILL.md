---
name: calculator
version: 1.1.0
description: 简易计算器 Skill，提供加法与减法运算
author: teage-liu
requires: []
---

# Calculator Skill

简易计算器示例 Skill。

## 工具调用方式

### 加法运算
执行 `python skills/calculator/scripts/calculator.py add {a} {b}`，返回两数之和。

示例:
```bash
python skills/calculator/scripts/calculator.py add 3 5
```
输出: `8.0`

### 减法运算
执行 `python skills/calculator/scripts/calculator.py subtract {a} {b}`，返回两数之差。

示例:
```bash
python skills/calculator/scripts/calculator.py subtract 10 4
```
输出: `6.0`

## 注意事项

- 仅支持加法与减法运算
- 输入参数会被转为 float 计算
- 如需查看脚本源码，调用 `skill__resource(name="calculator", rel_path="scripts/calculator.py")`
