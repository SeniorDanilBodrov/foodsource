import fs from 'node:fs/promises';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error('Usage: node export_xlsx.mjs input.json output.xlsx');

const payload = JSON.parse(await fs.readFile(inputPath, 'utf8'));
const suppliers = payload.suppliers || [];
const workbook = Workbook.create();
const sheet = workbook.worksheets.add('Поставщики');
sheet.showGridLines = false;

sheet.getRange('A1:R1').merge();
sheet.getRange('A1').values = [[payload.title || 'Выборка поставщиков FoodSource']];
sheet.getRange('A1').format = { font: { name: 'Arial', size: 15, bold: true, color: '#171B19' }, verticalAlignment: 'center' };
sheet.getRange('A2').values = [['Сформировано']];
sheet.getRange('B2').values = [[payload.generatedAt || '']];
sheet.getRange('A3').values = [['Количество поставщиков']];
sheet.getRange('B3').values = [[suppliers.length]];
sheet.getRange('A2:A3').format = { font: { name: 'Arial', size: 10, bold: true, color: '#5D675E' } };
sheet.getRange('B2:B3').format = { font: { name: 'Arial', size: 10, color: '#171B19' } };

const headers = ['Поставщик', 'Тип', 'Категории', 'Товары', 'Город', 'Регионы', 'Мин. заказ', 'Цена', 'Доставка', 'Документы', 'Телефон', 'Email', 'Адрес', 'Контакт', 'Сайт', 'Источник', 'Обновлено', 'Полнота, %'];
const rows = suppliers.map(s => [
  s.name, s.type, (s.categories || []).join('; '), (s.products || []).join('; '), s.city,
  (s.regions || []).join('; '), s.minimumOrder, s.priceRange, s.delivery,
  (s.certificates || []).join('; ') || 'Не указаны', s.phone || '', s.email || '', s.address || '',
  s.contact || 'Не указан', s.website, s.source, s.updated, s.completeness ?? 0,
]);
sheet.getRangeByIndexes(4, 0, rows.length + 1, headers.length).values = [headers, ...rows];
const tableRange = `A5:R${rows.length + 5}`;
const table = sheet.tables.add(tableRange, true, 'SuppliersExport');
table.style = 'TableStyleMedium2';
table.showBandedColumns = false;
sheet.getRange('A5:R5').format = { fill: '#171B19', font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' }, verticalAlignment: 'center', wrapText: true };
sheet.getRange(`A6:R${rows.length + 5}`).format = { font: { name: 'Arial', size: 10, color: '#222622' }, verticalAlignment: 'top', wrapText: true };
sheet.getRange(`R6:R${rows.length + 5}`).format.numberFormat = '0';
sheet.getRange('A:R').format.autofitColumns();
const widths = [26, 22, 25, 34, 23, 32, 18, 22, 32, 26, 20, 26, 35, 27, 30, 30, 14, 12];
widths.forEach((width, index) => { sheet.getRangeByIndexes(0, index, rows.length + 5, 1).format.columnWidth = width; });
sheet.getRange('A1:R1').format.rowHeight = 28;
sheet.getRange(`A5:R${rows.length + 5}`).format.autofitRows();
sheet.freezePanes.freezeRows(5);

workbook.recalculate();
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
