export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  const feeds = [
    { name: 'CNBC',         url: 'https://www.cnbc.com/id/100003114/device/rss/rss.html' },
    { name: 'MarketWatch',  url: 'https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines' },
    { name: 'AP',           url: 'https://feeds.apnews.com/rss/apf-business' },
    { name: 'Yahoo Finance',url: 'https://finance.yahoo.com/news/rssindex' },
    { name: 'Nasdaq',       url: 'https://www.nasdaq.com/feed/rssoutbound?category=Markets' },
    { name: 'BBC',          url: 'https://feeds.bbci.co.uk/news/business/rss.xml' },
    { name: 'FT',           url: 'https://www.ft.com/rss/home/uk' }
  ];
  const results = await Promise.allSettled(
    feeds.map(f => fetch(f.url, { headers: { 'User-Agent': 'Mozilla/5.0' } })
      .then(r => r.text())
      .then(xml => {
        const items = [];
        const matches = xml.matchAll(/<item[\s\S]*?<\/item>/g);
        for (const m of matches) {
          const title = (m[0].match(/<title><!\[CDATA\[(.*?)\]\]><\/title>/) || m[0].match(/<title>(.*?)<\/title>/))?.[1];
          const link = (m[0].match(/<link>(.*?)<\/link>/) || m[0].match(/<guid>(.*?)<\/guid>/))?.[1];
          if (title && link) items.push({ title: title.trim(), link: link.trim(), source: f.name });
          if (items.length >= 8) break;
        }
        return items;
      }))
  );
  const allItems = results.flatMap(r => r.status === 'fulfilled' ? r.value : []);
  res.status(200).json(allItems);
}
