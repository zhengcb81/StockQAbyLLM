#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向 llm_demo.py 插入缺失的方法"""

import sys

# 读取原文件
with open(r"C:\Users\郑曾波\Projects\StockQAbyLLM\llm_demo.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# 新方法
new_methods = '''
    def _build_growth_path_answer(self) -> str:
        """构建增长路径答案。"""
        return self._build_growth_answer()

    def _build_penetration_answer(self) -> str:
        """构建市场渗透率答案。"""
        return self._build_general_answer("市场渗透率")

    def _build_lifecycle_answer(self) -> str:
        """构建行业生命周期答案。"""
        return self._build_risk_answer()

    def _build_macro_factors_answer(self) -> str:
        """构建宏观因素答案。"""
        return self._build_development_answer()

    def _build_revenue_model_answer(self) -> str:
        """构建收入模式答案。"""
        return self._build_financial_answer()

    def _build_unit_economics_answer(self) -> str:
        """构建单位经济答案。"""
        return self._build_financial_answer()

    def _build_gross_margin_answer(self) -> str:
        """构建毛利率答案。"""
        return self._build_financial_answer()

    def _build_scale_economics_answer(self) -> str:
        """构建规模经济答案。"""
        return self._build_financial_answer()

    def _build_pricing_power_answer(self) -> str:
        """构建定价能力答案。"""
        return self._build_moat_answer()

    def _build_tech_uniqueness_answer(self) -> str:
        """构建技术独特性答案。"""
        return self._build_tech_answer()

    def _build_network_effects_answer(self) -> str:
        """构建网络效应答案。"""
        return self._build_moat_answer()

    def _build_switching_cost_answer(self) -> str:
        """构建切换成本答案。"""
        return self._build_moat_answer()

    def _build_scarse_resources_answer(self) -> str:
        """构建稀缺资源答案。"""
        return self._build_moat_answer()

    def _build_brand_power_answer(self) -> str:
        """构建品牌力答案。"""
        return self._build_market_position_answer()

    def _build_industry_competition_answer(self) -> str:
        """构建行业竞争答案。"""
        return self._build_market_position_answer()

    def _build_entry_threats_answer(self) -> str:
        """构建进入威胁答案。"""
        return self._build_risk_answer()

    def _build_market_scarcity_answer(self) -> str:
        """构建市场稀缺性答案。"""
        return self._build_market_position_answer()

    def _build_competitor_response_answer(self) -> str:
        """构建竞争对手反击答案。"""
        return self._build_market_position_answer()

    def _build_winner_takes_all_answer(self) -> str:
        """构建赢家通吃答案。"""
        return self._build_market_position_answer()

    def _build_channel_coverage_answer(self) -> str:
        """构建渠道覆盖答案。"""
        return self._build_market_position_answer()

    def _build_channel_control_answer(self) -> str:
        """构建渠道控制力答案。"""
        return self._build_market_position_answer()

    def _build_marketing_roi_answer(self) -> str:
        """构建营销ROI答案。"""
        return self._build_market_position_answer()

    def _build_customer_structure_answer(self) -> str:
        """构建客户结构答案。"""
        return self._build_financial_answer()

    def _build_service_delivery_answer(self) -> str:
        """构建服务交付答案。"""
        return self._build_market_position_answer()

    def _build_management_team_answer(self) -> str:
        """构建管理团队答案。"""
        return self._build_management_answer()

    def _build_management_integrity_answer(self) -> str:
        """构建管理层诚信答案。"""
        return self._build_management_answer()

    def _build_incentive_structure_answer(self) -> str:
        """构建激励结构答案。"""
        return self._build_management_answer()

    def _build_capital_allocation_answer(self) -> str:
        """构建资本配置答案。"""
        return self._build_management_answer()

    def _build_governance_answer(self) -> str:
        """构建公司治理答案。"""
        return self._build_management_answer()

    def _build_cashflow_answer(self) -> str:
        """构建现金流答案。"""
        return self._build_financial_answer()

    def _build_balance_sheet_answer(self) -> str:
        """构建资产负债表答案。"""
        return self._build_financial_answer()

    def _build_financial_transparency_answer(self) -> str:
        """构建财务透明度答案。"""
        return self._build_management_answer()

    def _build_regulatory_risk_answer(self) -> str:
        """构建监管风险答案。"""
        return self._build_risk_answer()

    def _build_tech_disruption_answer(self) -> str:
        """构建技术颠覆答案。"""
        return self._build_risk_answer()

    def _build_esg_risk_answer(self) -> str:
        """构建ESG风险答案。"""
        return self._build_risk_answer()

    def _build_supplier_dependency_answer(self) -> str:
        """构建供应商依赖答案。"""
        return self._build_risk_answer()

    def _build_business_resilience_answer(self) -> str:
        """构建业务韧性答案。"""
        return self._build_financial_answer()

'''

# 在 _build_general_answer 之前插入
insert_line = 726
for i, line in enumerate(lines):
    if "def _build_general_answer" in line:
        insert_line = i
        break

# 插入新方法
lines.insert(insert_line, new_methods)

# 写回文件
with open(r"C:\Users\郑曾波\Projects\StockQAbyLLM\llm_demo.py", "w", encoding="utf-8") as f:
    f.writelines(lines)

print(f"成功插入 {new_methods.count('def _build_')} 个新方法")
print(f"文件现在有 {len(lines)} 行")
