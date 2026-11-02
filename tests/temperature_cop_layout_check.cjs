// Optional geometry check; requires Playwright and an installed Chromium browser.
const {chromium}=require('playwright');
const path=require('path');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.HEI_CHROME});
  const page=await browser.newPage();
  await page.setContent('<!doctype html><html><body></body></html>');
  await page.addScriptTag({path:path.join(__dirname,'../custom_components/home_energy_intelligence/frontend/hei-panel.js')});
  for(const width of [320,360,390,430,768,1024,1366]){
    await page.setViewportSize({width,height:1200});
    const result=await page.evaluate(()=>{
      const Panel=customElements.get('home-energy-intelligence-panel'),panel=new Panel();
      panel.analysisData={mode:'year',period:'2026-01',source:{source_id:'out'},cop:{rows:[]}};
      panel.analysisMetrics=['vendor_heating_cop','primary_combined_cop'];
      panel.temperatureCopRows=()=>Array.from({length:12},(_,i)=>({key:`2026-${String(i+1).padStart(2,'0')}`,min:-10+i,max:12+i,mean:i===4?null:2+i,low:-8+i,high:10+i,coverage:90,valid:200,expected:300,qualities:[],cop:{vendor_heating_cop:2+i*.2,primary_combined_cop:3+i*.1}}));
      document.body.innerHTML='';document.body.style.margin='0';document.body.append(panel);
      panel.shadowRoot.innerHTML=`<style>${Panel.styles}</style><main>${panel.temperatureCopAnalysis()}</main>`;
      const root=panel.shadowRoot,charts=[...root.querySelectorAll('.analysis-plot')],container=root.querySelector('.analysis-scroll');
      const clipped=charts.flatMap(svg=>[...svg.querySelectorAll('text')].filter(t=>{const a=t.getBoundingClientRect(),b=svg.getBoundingClientRect();return a.width>0&&(a.left<b.left-1||a.right>b.right+1||a.top<b.top-1||a.bottom>b.bottom+1)})).length;
      return {overflow:document.documentElement.scrollWidth>innerWidth,charts:charts.length,heights:charts.map(c=>c.getBoundingClientRect().height),grids:charts.map(c=>c.querySelectorAll('.analysis-grid').length),clipped,scrollContained:container.scrollWidth>=container.clientWidth,tooltips:charts.map(c=>c.querySelectorAll('title').length)};
    });
    console.log(width,JSON.stringify(result));
    if(result.overflow||result.charts!==2||result.clipped||result.grids.some(x=>x<4)||result.tooltips.some(x=>!x)||result.heights.some(x=>Math.abs(x-(width<=800?250:270))>1))process.exitCode=1;
  }
  await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1});
