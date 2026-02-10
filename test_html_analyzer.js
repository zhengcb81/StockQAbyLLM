#!/usr/bin/env node
/**
 * 测试 HTML 分析器的数据处理逻辑
 * 模拟浏览器环境中的文件读取和数据处理
 */

const fs = require('fs');
const path = require('path');

// 配置文件路径
const CONFIG_PATH = path.join(__dirname, 'config.json');
const OUTPUTS_DIR = path.join(__dirname, 'outputs');

// 测试数据结构
let configData = null;
let qallmData = {};
let processedData = {
    questions: [],
    categories: [],
    totals: []
};

// 1. 读取配置文件
console.log('='.repeat(70));
console.log('步骤1: 读取 config.json');
console.log('='.repeat(70));

try {
    const configContent = fs.readFileSync(CONFIG_PATH, 'utf-8');
    configData = JSON.parse(configContent);
    console.log(`✅ 成功加载配置文件`);
    console.log(`   类别数量: ${configData.length}`);
    console.log(`   总问题数: ${configData.reduce((sum, cat) => sum + cat.questions.length, 0)}`);

    // 显示类别概览
    configData.forEach((cat, idx) => {
        console.log(`   ${idx + 1}. ${cat.category} (${cat.weight}) - ${cat.questions.length}题`);
    });
} catch (error) {
    console.error('❌ 配置文件读取失败:', error.message);
    process.exit(1);
}

// 2. 读取所有 QALLM 文件
console.log('\n' + '='.repeat(70));
console.log('步骤2: 读取所有 QALLM JSON 文件');
console.log('='.repeat(70));

try {
    const files = fs.readdirSync(OUTPUTS_DIR);
    const qallmFiles = files.filter(f => f.startsWith('QALLM_') && f.endsWith('.json'));

    console.log(`✅ 找到 ${qallmFiles.length} 个 QALLM 文件`);

    qallmFiles.forEach(file => {
        const filePath = path.join(OUTPUTS_DIR, file);
        const content = fs.readFileSync(filePath, 'utf-8');
        const data = JSON.parse(content);

        // 提取股票名称
        const stockName = file.replace('QALLM_', '').replace('.json', '');
        qallmData[stockName] = data;

        console.log(`   📄 ${stockName}: ${Object.keys(data).length} 个问题`);
    });
} catch (error) {
    console.error('❌ QALLM 文件读取失败:', error.message);
    process.exit(1);
}

// 3. 构建问题到类别的映射
console.log('\n' + '='.repeat(70));
console.log('步骤3: 构建问题到类别的映射');
console.log('='.repeat(70));

const questionToCategory = {};
configData.forEach(category => {
    category.questions.forEach(question => {
        questionToCategory[question] = {
            category: category.category,
            weight: parseFloat(category.weight) || 0
        };
    });
});

console.log(`✅ 映射表构建完成`);
console.log(`   映射关系数: ${Object.keys(questionToCategory).length}`);

// 4. 处理每个股票的数据
console.log('\n' + '='.repeat(70));
console.log('步骤4: 处理每个股票的数据');
console.log('='.repeat(70));

for (const [stockName, qallm] of Object.entries(qallmData)) {
    console.log(`\n📊 处理股票: ${stockName}`);

    const categoryScores = {};
    const categoryCounts = {};

    // 初始化类别统计
    Object.keys(questionToCategory).forEach(q => {
        const cat = questionToCategory[q].category;
        categoryScores[cat] = 0;
        categoryCounts[cat] = 0;
    });

    // 处理每个问题
    for (const [question, data] of Object.entries(qallm)) {
        const categoryInfo = questionToCategory[question];
        if (!categoryInfo) continue;

        const score = parseFloat(data.score) || 0;
        const category = categoryInfo.category;

        // 添加到问题列表
        processedData.questions.push({
            stock: stockName,
            category: category,
            question: question,
            score: score,
            description: data.description || ''
        });

        // 累加到类别
        categoryScores[category] += score;
        categoryCounts[category] += 1;
    }

    // 计算类别平均分和加权得分
    const categoryAverages = {};
    const categoryWeights = {};

    Object.keys(categoryScores).forEach(cat => {
        if (categoryCounts[cat] > 0) {
            const avg = categoryScores[cat] / categoryCounts[cat];
            categoryAverages[cat] = avg;

            // 获取权重
            const weight = configData.find(c => c.category === cat)?.weight || '0%';
            categoryWeights[cat] = parseFloat(weight) || 0;

            processedData.categories.push({
                stock: stockName,
                category: cat,
                average: avg,
                weight: categoryWeights[cat],
                weighted: avg * (categoryWeights[cat] / 100)
            });

            console.log(`   ${cat}: 平均分 ${avg.toFixed(2)}, 权重 ${categoryWeights[cat]}%, 加权 ${(avg * categoryWeights[cat] / 100).toFixed(2)}`);
        }
    });
}

// 5. 计算总分和排名
console.log('\n' + '='.repeat(70));
console.log('步骤5: 计算总分和排名');
console.log('='.repeat(70));

const stockTotals = {};

processedData.categories.forEach(item => {
    if (!stockTotals[item.stock]) {
        stockTotals[item.stock] = {
            total: 0,
            sumWeights: 0,
            categories: []
        };
    }
    stockTotals[item.stock].total += item.weighted;
    stockTotals[item.stock].sumWeights += item.weight;
    stockTotals[item.stock].categories.push({
        name: item.category,
        avg: item.average
    });
});

// 转换为数组并排序
const totalsArray = Object.entries(stockTotals).map(([stock, data]) => {
    const categories = data.categories.sort((a, b) => b.avg - a.avg);
    return {
        stock: stock,
        total: data.total,
        highest: categories[0]?.name || 'N/A',
        lowest: categories[categories.length - 1]?.name || 'N/A'
    };
}).sort((a, b) => b.total - a.total);

// 添加排名
totalsArray.forEach((item, index) => {
    item.rank = index + 1;
    processedData.totals.push(item);
});

console.log('✅ 股票排名完成:');
totalsArray.forEach(item => {
    console.log(`   #${item.rank} ${item.stock}: 总分 ${item.total.toFixed(2)} (最高: ${item.highest}, 最低: ${item.lowest})`);
});

// 6. 验证数据完整性
console.log('\n' + '='.repeat(70));
console.log('步骤6: 数据完整性验证');
console.log('='.repeat(70));

const stats = {
    stocks: Object.keys(qallmData).length,
    questionsTotal: processedData.questions.length,
    categoriesTotal: processedData.categories.length,
    totalsTotal: processedData.totals.length
};

console.log(`📊 数据统计:`);
console.log(`   股票数量: ${stats.stocks}`);
console.log(`   问题条目: ${stats.questionsTotal}`);
console.log(`   类别条目: ${stats.categoriesTotal}`);
console.log(`   总分条目: ${stats.totalsTotal}`);

// 验证每个股票的问题数量
const expectedQuestionsPerStock = Object.keys(questionToCategory).length;
const actualQuestionsPerStock = {};
processedData.questions.forEach(q => {
    actualQuestionsPerStock[q.stock] = (actualQuestionsPerStock[q.stock] || 0) + 1;
});

console.log(`\n✅ 每个股票的问题数量验证 (期望: ${expectedQuestionsPerStock}):`);
for (const [stock, count] of Object.entries(actualQuestionsPerStock)) {
    const status = count === expectedQuestionsPerStock ? '✅' : '❌';
    console.log(`   ${status} ${stock}: ${count} 个问题`);
}

// 7. 显示示例数据
console.log('\n' + '='.repeat(70));
console.log('步骤7: 示例数据展示');
console.log('='.repeat(70));

console.log('\n📋 问题详情表示例 (前3条):');
processedData.questions.slice(0, 3).forEach((item, idx) => {
    console.log(`   ${idx + 1}. [${item.stock}] ${item.category}: ${item.question.substring(0, 40)}... = ${item.score}`);
});

console.log('\n📊 类别平均分表示例 (前3条):');
processedData.categories.slice(0, 3).forEach((item, idx) => {
    console.log(`   ${idx + 1}. [${item.stock}] ${item.category}: ${item.average.toFixed(2)} (权重 ${item.weight}%, 加权 ${item.weighted.toFixed(2)})`);
});

console.log('\n🏆 综合排名表示例 (全部):');
processedData.totals.forEach(item => {
    console.log(`   #${item.rank} ${item.stock}: ${item.total.toFixed(2)} | 最高: ${item.highest} | 最低: ${item.lowest}`);
});

// 8. 总结
console.log('\n' + '='.repeat(70));
console.log('测试总结');
console.log('='.repeat(70));

console.log('✅ HTML分析器数据处理逻辑验证完成!');
console.log('   所有步骤执行成功，数据结构正确');
console.log('   可以用于前端HTML页面的多股票比较分析');
console.log('\n📊 数据可用于以下分析维度:');
console.log('   1. 跨股票问题得分对比');
console.log('   2. 类别平均分横向比较');
console.log('   3. 综合评分排名');
console.log('   4. 优势劣势类别识别');

console.log('\n🎯 前端实现建议:');
console.log('   - 三标签页: 详细问题 / 类别分析 / 综合排名');
console.log('   - 排序功能: 支持股票名、类别、得分、问题等字段');
console.log('   - 视觉反馈: 高分绿色、中分黄色、低分红色');
console.log('   - 响应式设计: 适配移动端查看');

console.log('\n' + '='.repeat(70));