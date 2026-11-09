import { readFile, writeFile } from 'node:fs/promises';

const countryGroups = new Map([
  ['RU', 'Русские'],
  ['UA', 'Украинские'],
  ['BY', 'Белорусские'],
]);
const other = 'Другие';
const groups = new Map([...countryGroups.values(), other].map(label => [label, []]));
const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' });

async function getChannelCountries() {
  const response = await fetch('https://iptv-org.github.io/api/channels.json', {
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error('Channel catalog: HTTP ' + response.status);
  const rows = await response.json();
  if (!Array.isArray(rows) || !rows.length) throw new Error('Empty channel catalog');
  return new Map(rows.map(row => [row.id.toLowerCase(), row.country]));
}

const [countries, playlist] = await Promise.all([
  getChannelCountries(),
  readFile('site/playlist.m3u', 'utf8'),
]);
const [header, ...lines] = playlist.trimEnd().split(/\r?\n/);
if (!header.startsWith('#EXTM3U')) throw new Error('Invalid M3U header');

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
  const id = attributes.match(/\btvg-id="([^"]+)"/i)?.[1].split('@', 1)[0].toLowerCase();
  const label = countryGroups.get(countries.get(id)) ?? other;
  const group = 'group-title="' + label + '"';
  const updated = /\bgroup-title="[^"]*"/i.test(attributes)
    ? attributes.replace(/\bgroup-title="[^"]*"/i, () => group)
    : attributes + ' ' + group;
  groups.get(label).push({ title, lines: [updated + ',' + title, ...options, line] });
  count++;
  entry = undefined;
}
if (entry || !count) throw new Error('Incomplete or empty playlist');

const output = [header];
for (const channels of groups.values()) {
  for (const channel of channels.sort((a, b) => collator.compare(a.title, b.title))) {
    output.push(...channel.lines);
  }
}
await writeFile('site/playlist-by-language.m3u', output.join('\n') + '\n');
console.log(JSON.stringify({
  channels: count,
  groups: Object.fromEntries([...groups].map(([label, channels]) => [label, channels.length])),
}));
