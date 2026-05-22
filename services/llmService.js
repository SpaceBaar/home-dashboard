const axios = require('axios');
const { BaseService } = require('../lib/BaseService');

/**
 * LLM Service (AI Insights) - Local Haillo-Ollama Provider
 */
class LLMService extends BaseService {

  constructor(cacheTTLMinutes = 90) {
    super({
      name: 'LLM',
      cacheKey: 'llm',
      cacheTTL: cacheTTLMinutes * 60 * 1000,
      retryAttempts: 3,
      retryCooldown: 300,
    });
  }

  /**
   * Check if the service is enabled.
   * Since this is hosted locally, we check if the endpoint is defined 
   * or default to localhost.
   */
  isEnabled() {
    const endpoint = process.env.LOCAL_LLM_URL || 'http://localhost:8000';
    return !!endpoint;
  }

  async fetchData(config, logger) {
    const baseUrl = process.env.LOCAL_LLM_URL || 'http://localhost:8000';
    const modelName = process.env.LOCAL_LLM_MODEL || 'qwen2:1.5b';

    const { systemPrompt, userMessage } = this.buildPrompt(config.input);
    
    logger.info?.(`[LLM] Calling Local hailo-ollama Chat API using ${modelName}`);

    try {
      const response = await axios.post(
        `${baseUrl}/api/chat`, // 1. Use chat endpoint for explicit template adherence
        {
          model: modelName,
          messages: [
            { role: 'system', content: systemPrompt },
            { role: 'user', content: userMessage }
          ],
          stream: false,
          options: {
            temperature: 0.1, // Near-zero temperature minimizes wandering behavior
            keep_alive: -1
          }
        },
        {
          timeout: 30000,
          headers: { 'Content-Type': 'application/json' },
        }
      );

      const rawText = response?.data?.message?.content || '';
      logger.info?.('[LLM] Raw Response:', rawText);

      let parsed = { clothing_suggestion: "Layers recommended", daily_summary: "Weather dashboard active" };

      // 2. LINE-BY-LINE SCRAPER (Highly reliable for small models)
      const lines = rawText.split('\n');
      for (let line of lines) {
        const cleanLine = line.trim();
        if (cleanLine.toUpperCase().startsWith('CLOTHING:')) {
          parsed.clothing_suggestion = cleanLine.replace(/^CLOTHING:\s*/i, '').trim();
        }
        if (cleanLine.toUpperCase().startsWith('SUMMARY:')) {
          parsed.daily_summary = cleanLine.replace(/^SUMMARY:\s*/i, '').trim();
        }
      }

      // Strict post-processing clean up for the e-paper matrix layout
      if (parsed.daily_summary) {
        parsed.daily_summary = parsed.daily_summary.replace(/[.!?,]+$/, '').trim();
        if (parsed.daily_summary.length > 78) {
          parsed.daily_summary = parsed.daily_summary.substring(0, 78).split(' ').slice(0, -1).join(' ');
        }
      }

      return {
        ...parsed,
        _meta: {
          input_tokens: response?.data?.prompt_eval_count || 0,
          output_tokens: response?.data?.eval_count || 0,
          cost_usd: 0.00,
          prompt: `SYSTEM:\n${systemPrompt}\n\nUSER:\n${userMessage}`,
        }
      };

    } catch (e) {
      logger.error?.('[LLM] Critical Transport/Inference Failure:', e.message);
      return {
        clothing_suggestion: "Check display text",
        daily_summary: "Local inference connection error pending retry loop",
        _meta: { input_tokens: 0, output_tokens: 0, cost_usd: 0, prompt: "" }
      };
    }
  }

  mapToDashboard(apiData, config) {
    return {
      clothing_suggestion: apiData.clothing_suggestion,
      daily_summary: apiData.daily_summary,
      _meta: apiData._meta,
    };
  }

  /**
   * Refactored for local metrics tracker
   */
  getCostInfo() {
    const cached = this.getCache(true);
    if (!cached || !cached._meta) return null;

    const { input_tokens, output_tokens, cost_usd, prompt } = cached._meta;
    const cacheTTLHours = this.cacheTTL / (1000 * 60 * 60);
    const callsPerDay = (24 - 5) / cacheTTLHours;

    return {
      last_call: {
        input_tokens,
        output_tokens,
        total_tokens: input_tokens + output_tokens,
        cost_usd,
        prompt,
      },
      projections: {
        calls_per_day: Math.round(callsPerDay * 10) / 10,
        daily_cost_usd: 0,
        monthly_cost_usd: 0,
      }
    };
  }

  buildPrompt({ current, forecast, hourlyForecast, location, timezone, sun, moon, air_quality }) {
    const timeContext = this.getTimeContext();
    const isNight = timeContext.period === 'night';
    const hoursToShow = timeContext.period === 'morning' ? 8 : 6;
    const relevantHourly = isNight ? hourlyForecast : hourlyForecast.slice(0, hoursToShow);
    const relevantForecast = forecast?.[0];

    const weatherContext = this.buildWeatherContext({
      current,
      relevantForecast,
      relevantHourly,
      isNight,
      moon,
      air_quality,
      timeContext
    });

    // A SIMPLIFIED, NON-JSON PROMPT DESIGNED FOR <2B CHAT MODELS
    const systemPrompt = `You are an automated assistant providing weather text for an e-ink kitchen display dashboard. 
The dashboard shows numerical values, so describe the FEEL and STORY of the weather.

You MUST format your output exactly as two lines:
CLOTHING: <practical advice, max 6 words>
SUMMARY: <vivid weather description between 60 and 78 characters total, with NO trailing punctuation>

Rules:
- DO NOT mention specific temperatures or degrees. Use words like "cool", "warm", "hot", "chilly", "mild".
- DO NOT mention specific months, dates, or weekdays.
- Do not write introductory prose or conversation. Start immediately with CLOTHING:

Example Output:
CLOTHING: Warm layers and rain gear
SUMMARY: Dreary and rainy most of the day with a cold evening breeze ahead
`;

    const now = new Date();
    const month = now.toLocaleString('default', { month: 'long' });
    const day = now.getDate();
    const hour = now.getHours();
    const ampm = hour < 12 ? 'AM' : 'PM';
    const time = `${hour % 12}:${String(now.getMinutes()).padStart(2, '0')} ${ampm}`;

    const userMessage = `Today is ${month} ${day}. It is ${timeContext.period.toUpperCase()}, ${time}. Planning for ${timeContext.planningFocus}

CURRENT WEATHER: ${current?.temp_f}°F, ${current?.description}
${weatherContext.dailyInfo}

HOURLY FORECAST:
${weatherContext.hourlyData}${weatherContext.contextNotes ? '\n\nNOTES: ' + weatherContext.contextNotes : ''}`;

    return { systemPrompt, userMessage };
  }

  buildWeatherContext({ current, relevantForecast, relevantHourly, isNight, moon, air_quality, timeContext }) {
    // Keep your exact buildWeatherContext logic intact...
    console.log(relevantHourly);
    const context = { contextNotes: [] };
    const maxRainChance = Math.max(relevantForecast?.rain_chance || 0, ...relevantHourly.map(h => h.rain_chance || 0));
    const rainMention = maxRainChance > 0 ? `, ${maxRainChance}% rain` : '';

    context.dailyInfo = isNight
      ? `TOMORROW: High ${relevantForecast?.high}°, Low ${relevantForecast?.low}°${rainMention}`
      : `TODAY: High ${relevantForecast?.high}°, Low ${relevantForecast?.low}°${rainMention}`;

    context.hourlyData = relevantHourly
      .map(h => `${h.time}: ${h.temp}° ${h.condition.trim()}${h.rain_chance > 0 ? ` (${h.rain_chance}%)` : ''}`)
      .join('\n');

    const temps = relevantHourly.map(h => h.temp);
    const tempRange = Math.max(...temps) - Math.min(...temps);
    if (tempRange >= 15) context.contextNotes.push(`${tempRange}° temperature swing`);

    const maxWind = Math.max(...relevantHourly.map(h => h.wind_mph || 0));
    if (maxWind >= 12) context.contextNotes.push(`Windy, gusts ${maxWind} mph`);

    const humidity = current?.humidity;
    if (humidity >= 80) context.contextNotes.push(`Humid (${humidity}%, muggy feel)`);
    else if (humidity <= 30) context.contextNotes.push(`Dry (${humidity}%, crisp feel)`);

    const conditions = relevantHourly.map(h => h.condition.trim().toLowerCase());
    const uniqueConditions = [...new Set(conditions)];

    if (uniqueConditions.length > 1) {
      const firstCond = conditions[0];
      const lastCond = conditions[conditions.length - 1];
      const transitionIndex = conditions.findIndex((c, i) => i > 0 && c !== conditions[i - 1]);
      if (transitionIndex > 0) {
        context.contextNotes.push(`${firstCond} → ${lastCond} around ${relevantHourly[transitionIndex].time}`);
      } else if (firstCond !== lastCond) {
        context.contextNotes.push(`${firstCond} → ${lastCond}`);
      }
    }

    if (moon && (timeContext.period === 'evening' || timeContext.period === 'night')) {
      if (moon.phase === 'full' || moon.illumination >= 95) context.contextNotes.push('Full moon (bright night)');
      else if (moon.phase === 'new' || moon.illumination <= 5) context.contextNotes.push('New moon');
      else if (moon.illumination >= 50 && moon.direction === 'waxing') context.contextNotes.push(`Bright ${moon.phase.replace('_', ' ')} moon`);
    }

    if (air_quality?.aqi > 100) context.contextNotes.push(`AQI ${air_quality.aqi} (${air_quality.category})`);

    const fogHours = relevantHourly.filter(h => h.condition.toLowerCase().includes('fog') || h.condition.toLowerCase().includes('mist'));
    if (fogHours.length >= 2) context.contextNotes.push(`Marine layer ${fogHours[0].time}-${fogHours[fogHours.length-1].time}`);

    const hotHours = relevantHourly.filter(h => h.temp >= 90);
    if (hotHours.length >= 2) context.contextNotes.push(`Heat peak ${hotHours[0].time}-${hotHours[hotHours.length-1].time}`);

    if (current?.feels_like_f && Math.abs(current.temp_f - current.feels_like_f) >= 5) {
      const delta = current.feels_like_f - current.temp_f;
      context.contextNotes.push(`Feels ${delta > 0 ? 'warmer' : 'cooler'} (${Math.abs(delta)}° diff)`);
    }

    context.contextNotes = context.contextNotes.slice(0, 5).join(' • ');
    return context;
  }

  getTimeContext() {
    // Keep your exact getTimeContext logic intact...
    const hour = new Date().getHours();
    if (hour >= 5 && hour < 11) {
      return { period: 'morning', planningFocus: 'the full day ahead. Describe how the day is starting and what to expect ahead. You MUST mention "today" or "this morning" once' };
    } else if (hour >= 11 && hour < 16) {
      return { period: 'afternoon', planningFocus: 'this afternoon and evening. Describe the current and upcoming conditions.' };
    } else if (hour >= 16 && hour < 20) {
      return { period: 'evening', planningFocus: 'tonight. Describe how the day is ending.' };
    } else {
      return { period: 'night', planningFocus: 'tomorrow. You MUST mention "tomorrow" once' };
    }
  }
}

module.exports = { LLMService };