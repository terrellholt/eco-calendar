export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');

  const FOUR_HOURS = 4 * 60 * 60 * 1000;
  const now = Date.now();

  const BLOCKLIST = [
    'opinion','editorial','commentary','analysis','explainer','here\'s why',
    'why you should','what to know','what you need','how to','the case for',
    'the case against','ranked','reviewed','best and worst','column','letter to',
    'perspective','deep dive','long read','special report','investigat'
  ];

  const ALLOWLIST = [
    'earnings','revenue','profit','loss','gdp','inflation','cpi','ppi','pce',
    'jobs','unemployment','payroll','nonfarm','fed','fomc','rate','rates',
    'interest rate','powell','treasury','yield','bond','tariff','tariffs',
    'sanctions','trade','deficit','surplus','ipo','merger','acquisition',
    'deal','bankruptcy','default','recession','growth','market','stocks',
    'nasdaq','s&p','dow','oil','gold','dollar','euro','bitcoin','crypto',
    'bank','central bank','ecb','boe','boj','imf','world bank','opec',
    'conflict','war','sanction','geopolit','election','policy','legislation',
    'regulation','sec','antitrust','split','buyback','dividend','guidance',
    'forecast','outlook','downgrade','upgrade','rally','selloff','crash',
    'surge','plunge','spike','slump','quarter','fiscal','annual'
  ];

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
          const link  = (m[0].match(/<link>(.*?)<\/link>/)  || m[0].match(/<guid>(.*?)<\/guid>/))?.[1];
          const pubDateStr = (m[0].match(/<pubDate>(.*?)<\/pubDate>/))?.[1];

          if (!title || !link) continue;

          if (pubDateStr) {
            const pubTime = new Date(pubDateStr).getTime();
            if (isNaN(pubTime) || now - pubTime > FOUR_HOURS) continue;
          } else {
            continue;
          }

          const t = title.toLowerCase();
          if (BLOCKLIST.some(w => t.includes(w))) continue;
          if (!ALLOWLIST.some(w => t.includes(w))) continue;

          items.push({ title: title.trim(), link: link.trim(), source: f.name });
          if (items.length >= 8) break;
        }
        return items;
      }))
  );

  const allItems = results.flatMap(r => r.status === 'fulfilled' ? r.value : []);
  res.status(200).json(allItems);
}
