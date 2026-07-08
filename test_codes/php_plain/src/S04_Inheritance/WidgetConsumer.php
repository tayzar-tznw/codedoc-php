<?php
declare(strict_types=1);

namespace App\S04_Inheritance;

class WidgetConsumer
{
    public function inspect(): array
    {
        $child = new ChildWidget();

        return [
            $child->inheritedOnly(),
            $child->format(),
            $child->describe(),
        ];
    }
}
