export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');

  const TWENTY_FOUR_HOURS = 24 * 60 * 60 * 1000;
  const MAX_ITEMS = 15;
  const now = Date.now();

  const BLOCKLIST = [
    'opinion','editorial','commentary','explainer','here\'s why',
    'why you should','what to know','what you need','how to','the case for',
    'the case against','ranked','reviewed','best and worst','column','letter to',
    'perspective','deep dive','long read','special report','investigat',
    'which is the better','which is better','here are','here\'s what',
    'may soon','might soon','could soon','what we\'re watching','things to watch',
    'things we\'re watching','big things','what happened this week',
    'week ahead','what to watch','looking ahead','what investors should',
    'should you buy','should you sell','is it time to','time to buy',
    'time to sell','demonstrates','study finds','clinical trial',
    'weight loss','bottom line','the bottom line',
    'buying the dip','we\'re buying','our newest','getting a better price'
  ];

  const SOURCE_BLOCKLIST = [
    'motley fool','fool.com','seeking alpha','investopedia','thestreet',
    'benzinga','zacks','marketbeat','yahoo'
  ];

  const ALLOWLIST = [
    'reports earnings','posted earnings','quarterly earnings','q1','q2','q3','q4',
    'beats estimates','misses estimates','raises guidance','cuts guidance',
    'gdp','cpi','ppi','pce','nonfarm payroll','unemployment rate','jobs report',
    'fed raises','fed cuts','rate decision','fomc','interest rate decision',
    'powell says','powell warns','central bank raises','central bank cuts',
    'treasury yield','10-year yield','2-year yield',
    'declares war','military strike','sanctions imposed','sanctions lifted',
    'trade deal','trade war','tariff imposed','tariff raised','tariff cut',
    'merger approved','acquisition completed','deal closed','ipo priced',
    'bankruptcy filed','files for bankruptcy','defaults on',
    'market closes','market opens','circuit breaker','trading halted',
    'opec cuts','opec raises','oil supply','oil output',
    'imf warns','world bank','g7','g20','emergency meeting',
    'sec charges','doj charges','antitrust','fined','indicted'
  ];

  const feeds = [
    { name: 'CNBC',        url: 'https://www.cnbc.com/id/100003114/device/rss/rss.html' },
    { name: 'MarketWatch', url: 'https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines' },
    { name: 'AP',          url: 'https://feeds.apnews.com/rss/apf-business' },
    { name: 'BBC',         url: 'https://feeds.bbci.co.uk/news/business/rss.xml' },
    { name: 'FT',          url: 'https://www.ft.com/rss/home/uk' },
    { name: 'Reuters',     url: 'https://finance.yahoo.com/news/rssindex' }
  ];

  const results = await Promise.allSettled(
    feeds.map(f => fetch(f.url, { headers: { 'User-Agent': 'Mozilla/5.0' } })
      .then(r => r.text())
      .then(xml => {
        const strict = [];
        const fallback = [];
        const matches = xml.matchAll(/<item[\s\S]*?<\/item>/g);
        for (const m of matches) {
          const title = (m[0].match(/<title><!\[CDATA\[(.*?)\]\]><\/title>/) || m[0].match(/<title>(.*?)<\/title>/))?.[1];
          const link  = (m[0].match(/<link>(.*?)<\/link>/)  || m[0].match(/<guid>(.*?)<\/guid>/))?.[1];
          const pubDateStr = (m[0].match(/<pubDate>(.*?)<\/pubDate>/))?.[1];

          if (!title || !link || !pubDateStr) continue;

          const pubTime = new Date(pubDateStr).getTime();
          if (isNaN(pubTime) || now - pubTime > TWENTY_FOUR_HOURS) continue;

          const t = title.toLowerCase();
          if (BLOCKLIST.some(w => t.includes(w))) continue;
          if (SOURCE_BLOCKLIST.some(w => link.toLowerCase().includes(w))) continue;

          const item = { title: title.trim(), link: link.trim(), source: f.name, pubTime };

          if (ALLOWLIST.some(w => t.includes(w))) {
            strict.push(item);
          } else {
            fallback.push(item);
          }
        }
        return { strict, fallback };
      }))
  );

  const allStrict   = results.flatMap(r => r.status === 'fulfilled' ? r.value.strict : []);
  const allFallback = results.flatMap(r => r.status === 'fulfilled' ? r.value.fallback : []);

  const pool = allStrict.length > 0 ? allStrict : allFallback;

  pool.sort((a, b) => b.pubTime - a.pubTime);

  const deduped = [];
  for (const item of pool) {
    const words = new Set(item.title.toLowerCase().split(/\s+/).filter(w => w.length > 4));
    const isDupe = deduped.some(existing => {
      const existingWords = new Set(existing.title.toLowerCase().split(/\s+/).filter(w => w.length > 4));
      const intersection = [...words].filter(w => existingWords.has(w)).length;
      return intersection / Math.max(words.size, existingWords.size) > 0.6;
    });
    if (!isDupe) deduped.push(item);
    if (deduped.length >= MAX_ITEMS) break;
  }

  res.status(200).json(deduped.map(({ pubTime, ...rest }) => rest));
}
