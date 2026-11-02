import test from 'node:test';
import assert from 'node:assert/strict';
import {calculatePlan, parseLevels, validURL} from '../rt_extractor/static/benchmark-common.mjs';

test('benchmark plan includes all warmups and the concurrency floor',()=>{
  assert.deepEqual(parseLevels('10，1 5, 5'),[1,5,10]);
  const p=calculatePlan({models:['a','b'],modes:['unique','shared-prefix'],concurrencyLevels:[1,5,10],requestsPerStage:20});
  assert.deepEqual(p,{models:2,modes:2,levels:3,stages:12,measuredPlanned:240,warmupPlanned:6,planned:246});
  assert.equal(calculatePlan({models:['a'],modes:['unique'],concurrencyLevels:[10],requestsPerStage:1}).planned,10);
  for(const bad of ['1,1.5','0,1','1e2','1 2 3 4 5 6 7 8 9'])assert.throws(()=>parseLevels(bad));
});
test('secret-bearing URLs are rejected before localStorage persistence',()=>{
  for(const value of ['https://user:secret@example.test','https://host/#','https://host/?api_key=secret','https://host/?t%256fken=secret','file:///tmp/x'])assert.throws(()=>validURL(value));
  assert.equal(validURL('http://localhost:9000/proxy/v1?api-version=stable').pathname,'/proxy/v1');
});
