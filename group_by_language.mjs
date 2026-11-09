import { readFile, writeFile } from 'node:fs/promises';

const api = 'https://iptv-org.github.io/api';
const displayNames = new Intl.DisplayNames('ru', { type: 'language', fallback: 'none' });
const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' });
const unknown = 'Язык не указан';

async function getCatalog(file) {
  const response = await fetch(`${api}/${file}.json`, { signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`${file}: HTTP ${response.status}`);
  const rows = await response.json();
  if (!Array.isArray(rows) || !rows.length) throw new Error(`Empty catalog: ${file}`);
  return rows;
}

const [feeds, languages, playlist] = await Promise.all([
  getCatalog('feeds'),
  getCatalog('languages'),
  readFile('site/playlist.m3u', 'utf8'),
]);
const feedLanguages = new Map(feeds.map(row => [
  `${row.channel}@${row.id}`.toLowerCase(), row.languages,
]));
const mainLanguages = new Map(feeds.filter(row => row.is_main).map(row => [
  row.channel.toLowerCase(), row.languages,
]));
const officialNames = new Map(languages.map(row => [row.code, row.name]));

function languageName(code) {
  if (!code || code === 'und' || code === 'zxx') return unknown;
  const name = displayNames.of(code) || officialNames.get(code);
  return name ? name[0].toLocaleUpperCase('ru') + name.slice(1) : unknown;
}

const [header, ...lines] = playlist.trimEnd().split(/\r?\n/);
if (!header.startsWith('#EXTM3U')) throw new Error('Invalid M3U header');

const groups = new Map();
let entry;
let count = 0;
for (const line of lines) {
  if (line.startsWith('#EXTINF:')) {
    if (entry) throw new Error('M3U entry is missing a stream URL');
    entry = [line];
    continue;
  }
  if (!entry || !line.trim()) continue;
  if (line.startsWith('#')) {
    entry.push(line);
    continue;
  }

  const [info, ...options] = entry;
  const [, attributes, title] = /^(#EXTINF:(?:"[^"]*"|[^",])*),(.*)$/.exec(info) ?? [];
  if (!attributes) throw new Error('Invalid EXTINF');
  const id = attributes.match(/\btvg-id="([^"]+)"/i)?.[1].toLowerCase();
  const codes = id?.includes('@') ? feedLanguages.get(id) : mainLanguages.get(id);
  const label = languageName(codes?.[0]);
  const group = `group-title="${label.replaceAll('"', "'")}"`;
  const updated = /\bgroup-title="[^"]*"/i.test(attributes)
    ? attributes.replace(/\bgroup-title="[^"]*"/i, () => group)
    : `${attributes} ${group}`;
  if (!groups.has(label)) groups.set(label, []);
  groups.get(label).push({ title, lines: [`${updated},${title}`, ...options, line] });
  count++;
  entry = undefined;
}
if (entry || !count) throw new Error('Incomplete or empty playlist');

const ordered = [...groups.keys()].sort((a, b) => {
  if (a === unknown) return b === unknown ? 0 : 1;
  if (b === unknown) return -1;
  return collator.compare(a, b);
});
const output = [header];
for (const label of ordered) {
  for (const channel of groups.get(label).sort((a, b) => collator.compare(a.title, b.title))) {
    output.push(...channel.lines);
  }
}
await writeFile('site/playlist-by-language.m3u', output.join('\n') + '\n');
console.log(JSON.stringify({
  channels: count,
  groups: groups.size,
  unknown: groups.get(unknown)?.length ?? 0,
  russian: groups.get('Русский')?.length ?? 0,
  english: groups.get('Английский')?.length ?? 0,
}));
