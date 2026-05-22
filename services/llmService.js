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
    // 1. Correct fallback model identifier
    const modelName = process.env.LOCAL_LLM_MODEL || 'qwen2:1.5b'; 

    const { systemPrompt, userMessage } = this.buildPrompt(config.input);
    
    logger.info?.(`[LLM] Calling Local hailo-ollama API using ${modelName}`);

    try {
      const response = await axios.post(
        `${baseUrl}/api/generate`,
        {
          model: modelName,
          system: systemPrompt,
          prompt: userMessage,
          format: 'json', 
          stream: false,
          options: {
            temperature: 0.5, // Slightly lower temperature improves structural adherence
          }
        },
        {
          timeout: 90000, // 2. Robust 90-second safety window for heavy initial processing loops
          headers: {
            'Content-Type': 'application/json',
          },
        }
      );

      const rawText = response?.data?.response || '';
      logger.info?.('[LLM] Raw Response:', rawText);

      const inputTokens = response?.data?.prompt_eval_count || 0;
      const outputTokens = response?.data?.eval_count || 0;
      const costUsd = 0.00; 

      logger.info?.(`[LLM] Tokens: ${inputTokens} input, ${outputTokens} output | Cost: $${costUsd.toFixed(2)}`);

      let parsed;
      // Standard structural validation cleaner
      let cleanText = rawText.replace(/```json\n?/g, '').replace(/```\n?/g, '').trim();
      const jsonStart = cleanText.indexOf('{');
      const jsonEnd = cleanText.lastIndexOf('}');

      if (jsonStart !== -1 && jsonEnd !== -1 && jsonEnd > jsonStart) {
        cleanText = cleanText.substring(jsonStart, jsonEnd + 1);
      }
      
      parsed = JSON.parse(cleanText);

      if (parsed.daily_summary) {
        parsed.daily_summary = parsed.daily_summary.trim().replace(/[.!?,]+$/, '');

        if (parsed.daily_summary.length > 78) {
          parsed.daily_summary = parsed.daily_summary.substring(0, 78).split(' ').slice(0, -1).join(' ');
        }
      }

      return {
        ...parsed,
        _meta: {
          input_tokens: inputTokens,
          output_tokens: outputTokens,
          cost_usd: costUsd,
          prompt: `SYSTEM:\n${systemPrompt}\n\nUSER:\n${userMessage}`,
        }
      };

    } catch (e) {
      // Catch network errors explicitly to differentiate from structural JSON parsing bugs
      logger.error?.('[LLM] Critical Transport/Inference Failure:', e.message);
      return {
        clothing_suggestion: "Check dashboard text",
        daily_summary: "Local inference engine connection error pending retry loops",
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
    // Keep your exact prompt building logic intact...
    const timeContext = this.getTimeContext();
    const isNight = timeContext.period === 'night';
    const hoursToShow = timeContext.period === 'morning' ? 8 : 6;
    console.log(hourlyForecast);
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

    const systemPrompt = `You generate accurate and helpful weather insights for a kitchen e-ink display. The dashboard shows temps/numbers, so describe the FEEL and STORY of the weather to help the user plan their day.

Return JSON:
{
  "clothing_suggestion": "practical clothingadvice, max 6 words",
  "daily_summary": "vivid weather narrative, 60-78 chars total (including spaces and punctuation), no ending punctuation"
}

Style:
- Comment specifically on things that are normal or out of the ordinary, help the user plan their day
- Write like a friendly late night weather reporter providing informative updates
- Keep observations factual and helpful
- Describe changes: "warming up", "heating up fast", "cooling down", "drying out", "getting wetter", "clearing up", "getting cloudy"

Rules:
- DO NOT mention specific temps (dashboard shows these) - use "cool", "warm", "hot", "chilly", "mild"
- DO NOT mention specific month or date, but you can describe the season (e.g. Summer, Spring, Fall, Winter)

Examples:
{"clothing_suggestion": "Warm layers and rain gear", "daily_summary": "Dreary and rainy most of the day. Rain not letting up, stay cozy and dry"}
{"clothing_suggestion": "Layers you can shed", "daily_summary": "Cool start warming up fast, sunny and pleasant by afternoon"}
{"clothing_suggestion": "Sweater for the day", "daily_summary": "Chilly and misty this morning, staying fairly cool throughout the day"}
{"clothing_suggestion": "Jacket for tonight", "daily_summary": "Breezy and mild now, cooling down with clear skies come evening"}
{"clothing_suggestion": "Light layers, potentially shorts weather", "daily_summary": "Tomorrow foggy and cool early, clearing to sunny skies and warm temperatures"}
{"clothing_suggestion": "Warm jacket and layers", "daily_summary": "Misty morning transforming into a gorgeous mild but sunny afternoon"}

Remember:
- Daily summary must be at least 60 characters and CANNOT be more than 78 total characters (including spaces and punctuation)
- You MUST return valid JSON ONLY
`;

    const now = new Date();
    const month = now.toLocaleString('default', { month: 'long' });
    const day = now.getDate();
    const hour = now.getHours();
    const ampm = hour < 12 ? 'AM' : 'PM';
    const time = `${hour % 12}:${String(now.getMinutes()).padStart(2, '0')} ${ampm}`;

    const userMessage = `Today is ${month} ${day}. It is ${timeContext.period.toUpperCase()}, ${time}. Planning for ${timeContext.planningFocus}

CURRENT WEATHER: ${current?.temp_f}°C, ${current?.description}
${weatherContext.dailyInfo}

HOURLY FORECAST:
${weatherContext.hourlyData}${weatherContext.contextNotes ? '\n\nNOTES: ' + weatherContext.contextNotes : ''}`;

    return { systemPrompt, userMessage };
  }

  buildWeatherContext({ current, relevantForecast, relevantHourly, isNight, moon, air_quality, timeContext }) {
    // Keep your exact buildWeatherContext logic intact...
    const context = { contextNotes: [] };
    const maxRainChance = Math.max(relevantForecast?.rain_chance || 0, ...relevantHourly.map(h => h.rain_chance || 0));
    const rainMention = maxRainChance > 0 ? `, ${maxRainChance}% rain` : '';

    context.dailyInfo = isNight
      ? `TOMORROW: High ${relevantForecast?.high}°, Low ${relevantForecast?.low}°${rainMention}`
      : `TODAY: High ${relevantForecast?.high}°, Low ${relevantForecast?.low}°${rainMention}`;

    context.hourlyData = relevantHourly
      .map(h => `${h.time}: ${h.temp_f}° ${h.condition.trim()}${h.rain_chance > 0 ? ` (${h.rain_chance}%)` : ''}`)
      .join('\n');

    const temps = relevantHourly.map(h => h.temp_f);
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

    const hotHours = relevantHourly.filter(h => h.temp_f >= 90);
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