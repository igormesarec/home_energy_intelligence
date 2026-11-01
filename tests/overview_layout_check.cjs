// Optional visual geometry check; requires an installed browser and Playwright.
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.HEI_CHROME});
  const page = await browser.newPage();
  await page.setContent('<!doctype html><html><body></body></html>');
  await page.addScriptTag({path:path.join(__dirname,'../custom_components/home_energy_intelligence/frontend/hei-panel.js')});
  for (const width of [320,360,390,430,768,1024,1366,1920]) {
    await page.setViewportSize({width,height:1200});
    const result=await page.evaluate(() => {
      const Panel=customElements.get('home-energy-intelligence-panel');
      const panel=new Panel();
      panel.historyPreset='previous_month';panel.selectedPeriod={label:'October',start_date:'2026-10-01',end_date:'2026-10-31'};
      panel.historySummary={known_pv_kwh:20,energy:{equipment:[],space_heating_kwh:null,known_space_heating_kwh:4}};
      panel.historyCoverage={complete:false};panel.overviewData={};panel.responseProfileData={points:[]};
      panel.history=Array.from({length:6},(_,i)=>({local_date:`2026-10-${String(i+1).padStart(2,'0')}`,household_kwh:20+i,quality:'partial',energy:{equipment:[],known_space_heating_kwh:4,pv_kwh:5,import_kwh:12,export_kwh:2,household_kwh:20+i}}));
      panel.v02Report={whole_house_heating:panel.history.map(r=>({local_date:r.local_date,heat_pumps:[{electricity_kwh:3}],air_conditioners:[{electricity_kwh:1}],classification_complete:false}))};
      document.body.innerHTML='';document.body.style.margin='0';document.body.append(panel);
      panel.shadowRoot.innerHTML=`<style>${Panel.styles}</style><main>${panel.overview()}</main>`;
      const root=panel.shadowRoot,cards=[...root.querySelectorAll('.card')];
      const overlaps=cards.filter((a,i)=>cards.slice(i+1).some(b=>{const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return x.left<y.right-1&&x.right>y.left+1&&x.top<y.bottom-1&&x.bottom>y.top+1&&!a.contains(b)&&!b.contains(a);})).length;
      return {pageOverflow:document.documentElement.scrollWidth>innerWidth,overlaps,plots:root.querySelectorAll('.series-scroll').length};
    });
    console.log(width,JSON.stringify(result));
    if(result.pageOverflow||result.overlaps||result.plots!==2)process.exitCode=1;
  }
  await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1});
