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
      panel.historyCoverage={complete:false};panel.overviewData={representative_indoor_source_id:'indoor',representative_outdoor_source_id:'outdoor'};panel.responseProfileData={points:[]};
      panel.temperatureSources=[{source_id:'indoor',role:'indoor',enabled:true},{source_id:'outdoor',role:'outdoor',enabled:true}];
      panel.temperatureTimelineData={resolution:'daily',timezone:'UTC',start_date:'2026-10-01',end_date:'2026-10-05',range_start_utc:'2026-10-01T00:00:00Z',range_end_utc:'2026-10-06T00:00:00Z',source_ids:['indoor','outdoor'],sources:[],available_interval_count:5,expected_interval_count:5,axis_ticks:[],intervals:Array.from({length:5},(_,i)=>({local_date:`2026-10-0${i+1}`,local_label:`2026-10-0${i+1}`,start_utc:`2026-10-0${i+1}T00:00:00Z`,end_utc:`2026-10-0${i+2}T00:00:00Z`,temperatures:{indoor:{mean:20+i,minimum:18,maximum:25,valid_seconds:86400,expected_seconds:86400,coverage_percent:100},outdoor:{mean:5+i,minimum:1,maximum:12,valid_seconds:86400,expected_seconds:86400,coverage_percent:100}}}))};
      panel.history=Array.from({length:6},(_,i)=>({local_date:`2026-10-${String(i+1).padStart(2,'0')}`,household_kwh:20+i,quality:'partial',energy:{equipment:[],known_space_heating_kwh:4,pv_kwh:5,import_kwh:12,export_kwh:2,household_kwh:20+i}}));
      panel.v02Report={whole_house_heating:panel.history.map(r=>({local_date:r.local_date,heat_pumps:[{electricity_kwh:3}],air_conditioners:[{electricity_kwh:1}],classification_complete:false}))};
      document.body.innerHTML='';document.body.style.margin='0';document.body.append(panel);
      panel.shadowRoot.innerHTML=`<style>${Panel.styles}</style><main>${panel.overview()}</main>`;
      const root=panel.shadowRoot,cards=[...root.querySelectorAll('.card')];
      const overlaps=cards.filter((a,i)=>cards.slice(i+1).some(b=>{const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return x.left<y.right-1&&x.right>y.left+1&&x.top<y.bottom-1&&x.bottom>y.top+1&&!a.contains(b)&&!b.contains(a);})).length;
      const clipped=[...root.querySelectorAll('svg text')].filter(t=>{const a=t.getBoundingClientRect(),b=t.closest('svg').getBoundingClientRect();return a.width>0&&(a.left<b.left-1||a.right>b.right+1||a.top<b.top-1||a.bottom>b.bottom+1)}).length;
      const internal=[...root.querySelectorAll('.card')].filter(c=>c.scrollWidth>c.clientWidth+1).length;
      return {pageOverflow:document.documentElement.scrollWidth>innerWidth,overlaps,clipped,internal,plots:root.querySelectorAll('.series-scroll').length,temperatures:root.querySelectorAll('.temp-chart').length};
    });
    console.log(width,JSON.stringify(result));
    if(result.pageOverflow||result.overlaps||result.clipped||result.internal||result.plots!==2||result.temperatures!==2)process.exitCode=1;
  }
  await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1});
