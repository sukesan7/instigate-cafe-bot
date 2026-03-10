import asyncio
from icbot.features.grades import fetch_course_data, generate_chart

async def main():
    course = 'ele888'
    data = await fetch_course_data(course)
    print('Fetched:', data['title'] if data else 'None')
    if data:
        buf = generate_chart(data['title'], data['professors'])
        print('Chart generated, bytes:', buf.getbuffer().nbytes)
        # Test pulling from cache
        print('Fetching from cache...')
        data2 = await fetch_course_data(course)
        print('Cache returned:', len(data2['professors']) if data2 else 'None', 'professors')

if __name__ == "__main__":
    asyncio.run(main())
